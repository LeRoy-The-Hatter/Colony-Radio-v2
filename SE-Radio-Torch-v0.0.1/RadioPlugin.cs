using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using NLog;
using Sandbox.Game.World;
using Sandbox.Game.Entities;
using Sandbox.Game.Entities.Cube;
using Sandbox.ModAPI;
using Torch;
using Torch.API;
using Torch.API.Plugins;
using Torch.API.Session;
using Torch.Session;
using VRageMath;
using VRage.Game.Entity;
using VRage.Game.ModAPI;
using VRage.ModAPI;

namespace SERadioTorch
{
    /// <summary>
    /// Torch plugin that periodically sends active player GUIDs and positions
    /// to the SE Radio UDP server using the existing CTRL_POSITION control frame.
    /// </summary>
    public class RadioPlugin : TorchPluginBase
    {
        private const byte ProtocolVersion = 1;
        private const byte MessageTypeControl = 1; // MT_CTRL
        private const byte ControlPosition = 5;    // CTRL_POSITION
        private const int HeaderSize = 12;         // !BBHII -> 1 + 1 + 2 + 4 + 4
        private const int ControlHeaderSize = 3;   // !BH   -> 1 + 2

        private static readonly Logger Log = LogManager.GetCurrentClassLogger();

        private ITorchBase _torch;
        private Persistent<RadioPluginConfig> _config;
        private Persistent<RadioInstanceIdentity> _instanceIdentity;
        private string _instanceIdentityPath;
        private TorchSessionManager _sessionManager;
        private CancellationTokenSource _loopCts;
        private readonly object _udpLock = new object();
        private UdpClient _udpClient;
        private readonly Stopwatch _clock = Stopwatch.StartNew();
        private int _seq;
        private string _serverName = "default";
        private uint _serverSsrc = 1;

        public override void Init(ITorchBase torch)
        {
            base.Init(torch);

            _torch = torch;
            var cfgPath = Path.Combine(StoragePath, "RadioPlugin.cfg");
            _config = Persistent<RadioPluginConfig>.Load(cfgPath);
            _config.Data.Clamp();
            _config.Save();

            _instanceIdentityPath = ResolveInstanceIdentityPath();
            RefreshInstanceIdentity();

            // Older Torch builds expose only the non-generic GetManager(Type) API
            _sessionManager = torch.Managers.GetManager(typeof(TorchSessionManager)) as TorchSessionManager;
            if (_sessionManager != null)
                _sessionManager.SessionStateChanged += OnSessionStateChanged;

            Log.Info("SE Radio Torch plugin initialized. Config at {0}; identity at {1}", cfgPath, _instanceIdentityPath);
        }

        public override void Dispose()
        {
            base.Dispose();
            StopLoop();
            if (_sessionManager != null)
                _sessionManager.SessionStateChanged -= OnSessionStateChanged;
            lock (_udpLock)
            {
                _udpClient?.Dispose();
                _udpClient = null;
            }
        }

        private void OnSessionStateChanged(ITorchSession session, TorchSessionState state)
        {
            switch (state)
            {
                case TorchSessionState.Loaded:
                    RefreshInstanceIdentity();
                    StartLoop();
                    break;
                case TorchSessionState.Unloading:
                case TorchSessionState.Unloaded:
                    StopLoop();
                    break;
            }
        }

        private void StartLoop()
        {
            if (_config?.Data == null || !_config.Data.Enabled)
            {
                Log.Info("Position forwarder is disabled in config; not starting loop.");
                return;
            }

            StopLoop();
            RefreshInstanceIdentity();
            EnsureUdpClient();

            _loopCts = new CancellationTokenSource();
            var token = _loopCts.Token;
            Task.Run(() => LoopAsync(token), token);

            Log.Info("Started position forwarder -> {0}:{1} every {2} ms",
                _config.Data.ServerHost, _config.Data.ServerPort, _config.Data.UpdateIntervalMs);
        }

        private void StopLoop()
        {
            if (_loopCts == null)
                return;
            try
            {
                _loopCts.Cancel();
            }
            catch (Exception)
            {
                // ignore
            }
            _loopCts.Dispose();
            _loopCts = null;
        }

        private void EnsureUdpClient()
        {
            lock (_udpLock)
            {
                _udpClient?.Dispose();
                _udpClient = new UdpClient();
                _udpClient.Connect(_config.Data.ServerHost, _config.Data.ServerPort);
            }
        }

        private async Task LoopAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    ForwardPlayerPositions();
                }
                catch (Exception ex)
                {
                    Log.Warn(ex, "Position forward loop error");
                }

                try
                {
                    await Task.Delay(_config.Data.UpdateIntervalMs, token).ConfigureAwait(false);
                }
                catch (TaskCanceledException)
                {
                    break;
                }
            }
        }

        private void ForwardPlayerPositions()
        {
            if (MySession.Static == null || MySession.Static.Players == null)
                return;

            // Ship player positions
            var players = MySession.Static.Players.GetOnlinePlayers();
            if (players == null)
                return;
            var playerCount = players.Count;
            var sentPlayers = 0;

            foreach (var player in players)
            {
                try
                {
                    SendPositionForPlayer(player);
                    sentPlayers++;
                }
                catch (Exception ex)
                {
                    Log.Debug(ex, "Failed to send position for player");
                }
            }

            // Ship antenna positions as a separate snapshot
            var antennaCount = 0;
            try
            {
                antennaCount = SendAntennaSnapshot();
            }
            catch (Exception ex)
            {
                Log.Debug(ex, "Failed to send antenna snapshot");
            }

            // Lightweight debug telemetry so we can confirm activity.
            Log.Debug("Position forwarder tick: players={0} sent={1} antennas={2}", playerCount, sentPlayers, antennaCount);
        }

        private void SendPositionForPlayer(MyPlayer player)
        {
            if (player == null)
                return;

            // Skip bots / inactive characters.
            if (player.IsBot)
                return;

            var character = player.Character;
            if (character == null || character.IsDead)
                return;

            Vector3D pos = character.PositionComp?.GetPosition() ?? Vector3D.Zero;
            var guid = player.Id.SteamId.ToString();

            var playerSsrc = DerivePlayerSsrc(player.Id.SteamId);
            var payload = new
            {
                server = _serverName,
                server_name = _serverName,
                server_ssrc = _serverSsrc,
                player_ssrc = playerSsrc,
                guid,
                steam_id = player.Id.SteamId,
                identity_id = player.Identity?.IdentityId ?? 0,
                position = new { x = pos.X, y = pos.Y, z = pos.Z }
            };

            var body = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(payload));
            var packet = BuildControlPacket(ControlPosition, body, playerSsrc);
            SendPacket(packet);
        }

        private int SendAntennaSnapshot()
        {
            var antennas = CollectAntennaPositions();
            var payload = new
            {
                server = _serverName,
                server_name = _serverName,
                server_ssrc = _serverSsrc,
                type = "antenna_snapshot",
                count = antennas.Count,
                antennas
            };
            var body = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(payload));
            // Use a per-server SSRC for antenna snapshots so multiple shards don't stomp each other.
            var antennaSsrc = DeriveAntennaSsrc();
            var packet = BuildControlPacket(ControlPosition, body, antennaSsrc);
            SendPacket(packet);
            return antennas.Count;
        }

        private List<object> CollectAntennaPositions()
        {
            var list = new List<object>();
            var grids = new HashSet<IMyEntity>();
            try
            {
                // Gather grids first, then query their terminal systems for antennas.
                MyAPIGateway.Entities.GetEntities(grids, e => e is IMyCubeGrid);
            }
            catch (Exception ex)
            {
                Log.Debug(ex, "Failed to enumerate grids for antenna scan");
                return list;
            }

            var seen = new HashSet<long>();
            foreach (var ent in grids)
            {
                if (!(ent is IMyCubeGrid grid))
                    continue;

                IMyGridTerminalSystem gts = null;
                try
                {
                    gts = MyAPIGateway.TerminalActionsHelper.GetTerminalSystemForGrid(grid);
                }
                catch (Exception ex)
                {
                    Log.Debug(ex, "Terminal system lookup failed for grid {0}", grid.DisplayName);
                }
                if (gts == null)
                    continue;

                var antennaBlocks = new List<IMyTerminalBlock>();
                try
                {
                    gts.GetBlocksOfType<IMyRadioAntenna>(antennaBlocks, block =>
                    {
                        var antBlock = block as IMyRadioAntenna;
                        return antBlock != null && antBlock.Enabled && antBlock.IsFunctional && antBlock.IsWorking;
                    });
                }
                catch (Exception ex)
                {
                    Log.Debug(ex, "Failed to enumerate antennas on grid {0}", grid.DisplayName);
                    continue;
                }

                foreach (var block in antennaBlocks)
                {
                    if (!(block is IMyRadioAntenna ant))
                        continue;
                    if (!seen.Add((long)ant.EntityId))
                        continue;

                    var pos = ant.PositionComp?.GetPosition() ?? Vector3D.Zero;
                    var gridName = grid?.DisplayName ?? grid?.Name ?? string.Empty;
                    list.Add(new
                    {
                        id = ant.EntityId,
                        name = ant.CustomName,
                        grid = gridName,
                        position = new { x = pos.X, y = pos.Y, z = pos.Z }
                    });
                }
            }

            return list;
        }

        private void SendPacket(byte[] packet)
        {
            if (packet == null || packet.Length == 0)
                return;
            lock (_udpLock)
            {
                if (_udpClient == null)
                    return;
                try
                {
                    _udpClient.Send(packet, packet.Length);
                }
                catch (Exception ex)
                {
                    Log.Debug(ex, "UDP send failed");
                }
            }
        }

        private byte[] BuildControlPacket(byte ctrlCode, byte[] body, uint ssrc)
        {
            if (body == null)
                body = Array.Empty<byte>();
            var buffer = new byte[HeaderSize + ControlHeaderSize + body.Length];
            buffer[0] = ProtocolVersion;
            buffer[1] = MessageTypeControl;
            BinaryPrimitives.WriteUInt16BigEndian(buffer.AsSpan(2, 2), NextSeq());
            BinaryPrimitives.WriteUInt32BigEndian(buffer.AsSpan(4, 4), Timestamp48k());
            BinaryPrimitives.WriteUInt32BigEndian(buffer.AsSpan(8, 4), ssrc);

            buffer[12] = ctrlCode;
            BinaryPrimitives.WriteUInt16BigEndian(buffer.AsSpan(13, 2), (ushort)body.Length);
            body.CopyTo(buffer.AsSpan(HeaderSize + ControlHeaderSize));

            return buffer;
        }

        private ushort NextSeq()
        {
            return (ushort)(Interlocked.Increment(ref _seq) & 0xFFFF);
        }

        private uint Timestamp48k()
        {
            return (uint)(_clock.Elapsed.TotalSeconds * 48000.0);
        }

        private static uint DeriveSsrc(string input)
        {
            // Deterministic FNV-1a hash so each input has a stable SSRC.
            if (string.IsNullOrWhiteSpace(input))
                return 1;

            unchecked
            {
                const uint offset = 2166136261;
                const uint prime = 16777619;
                uint hash = offset;
                foreach (char c in input)
                {
                    hash ^= c;
                    hash *= prime;
                }

                return hash == 0 ? 1u : hash;
            }
        }

        private uint DerivePlayerSsrc(ulong steamId)
        {
            // Salt player SSRCs with the server SSRC so cross-server collisions are avoided.
            var baseInput = $"{_serverSsrc}:{steamId}";
            var ssrc = DeriveSsrc(baseInput);
            if (ssrc == 0 || ssrc == _serverSsrc)
                ssrc = DeriveSsrc($"{baseInput}:player");
            return ssrc == 0 ? 1u : ssrc;
        }

        private uint DeriveAntennaSsrc()
        {
            // Antenna snapshot uses the configured server SSRC directly.
            return _serverSsrc == 0 ? 1u : _serverSsrc;
        }

        private void RefreshInstanceIdentity()
        {
            var fallbackName = GetDefaultServerName();
            var path = ResolveInstanceIdentityPath();

            try
            {
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrWhiteSpace(dir))
                    Directory.CreateDirectory(dir);

                _instanceIdentity = Persistent<RadioInstanceIdentity>.Load(path);
                if (_instanceIdentity.Data == null)
                    _instanceIdentity.Data = new RadioInstanceIdentity();

                _instanceIdentity.Data.Clamp(fallbackName, name => DeriveSsrc($"SERVER:{name}"));
                _instanceIdentity.Save();

                var name = string.IsNullOrWhiteSpace(_instanceIdentity.Data.ServerName)
                    ? "default"
                    : _instanceIdentity.Data.ServerName;
                var ssrc = _instanceIdentity.Data.ServerSsrc == 0 ? 1u : _instanceIdentity.Data.ServerSsrc;

                if (!string.Equals(name, _serverName, StringComparison.Ordinal) || ssrc != _serverSsrc)
                {
                    Log.Info("Using server identity '{0}' (SSRC {1})", name, ssrc);
                    _serverName = name;
                    _serverSsrc = ssrc;
                }
            }
            catch (Exception ex)
            {
                var fallbackSsrc = DeriveSsrc($"SERVER:{fallbackName}");
                if (!string.Equals(fallbackName, _serverName, StringComparison.Ordinal) || _serverSsrc != fallbackSsrc)
                {
                    Log.Warn(ex, "Failed to load instance identity; falling back to '{0}' (SSRC {1})", fallbackName, fallbackSsrc);
                }

                _serverName = string.IsNullOrWhiteSpace(fallbackName) ? "default" : fallbackName;
                _serverSsrc = fallbackSsrc == 0 ? 1u : fallbackSsrc;
            }
        }

        private string ResolveInstanceIdentityPath()
        {
            if (!string.IsNullOrWhiteSpace(_instanceIdentityPath))
                return _instanceIdentityPath;

            var root = ResolveInstanceRoot();
            _instanceIdentityPath = Path.Combine(root, "RadioInstanceIdentity.json");
            return _instanceIdentityPath;
        }

        private string ResolveInstanceRoot()
        {
            var torch = _torch;

            // Try reflection on common properties to avoid hard dependency on Torch versions.
            var direct = TryReadStringProperty(torch, "InstancePath");
            if (!string.IsNullOrWhiteSpace(direct))
                return direct;

            var configObj = TryGetProperty(torch, "Config");
            var cfgPath = TryReadStringProperty(configObj, "InstancePath");
            if (!string.IsNullOrWhiteSpace(cfgPath))
                return cfgPath;

            var cfgName = TryReadStringProperty(configObj, "InstanceName");
            if (!string.IsNullOrWhiteSpace(cfgName))
            {
                try
                {
                    return Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "Instance", cfgName);
                }
                catch (Exception ex)
                {
                    Log.Debug(ex, "Failed to build instance path from InstanceName");
                }
            }

            try
            {
                var parent = Directory.GetParent(StoragePath)?.Parent?.FullName;
                if (!string.IsNullOrWhiteSpace(parent))
                    return parent;
            }
            catch (Exception ex)
            {
                Log.Debug(ex, "Failed to derive instance path from storage");
            }

            return AppDomain.CurrentDomain.BaseDirectory;
        }

        private static object TryGetProperty(object obj, string propertyName)
        {
            try
            {
                return obj?.GetType().GetProperty(propertyName)?.GetValue(obj);
            }
            catch
            {
                return null;
            }
        }

        private static string TryReadStringProperty(object obj, string propertyName)
        {
            try
            {
                var value = obj?.GetType().GetProperty(propertyName)?.GetValue(obj);
                var str = value as string;
                if (!string.IsNullOrWhiteSpace(str))
                    return str;
            }
            catch
            {
                // ignore
            }

            return null;
        }

        private string GetDefaultServerName()
        {
            var sessionName = TryGetSessionName();
            if (!string.IsNullOrWhiteSpace(sessionName))
                return sessionName;

            var cfgTag = _config?.Data?.ServerTag;
            if (!string.IsNullOrWhiteSpace(cfgTag) &&
                !string.Equals(cfgTag, "default", StringComparison.OrdinalIgnoreCase))
            {
                return cfgTag.Trim();
            }

            return "default";
        }

        private string TryGetSessionName()
        {
            // Session/world name (typically matches the server name for DS).
            try
            {
                var sessionName = MySession.Static?.Name;
                if (!string.IsNullOrWhiteSpace(sessionName))
                    return sessionName.Trim();
            }
            catch (Exception ex)
            {
                Log.Debug(ex, "Failed to read session name");
            }

            return null;
        }
    }
}
