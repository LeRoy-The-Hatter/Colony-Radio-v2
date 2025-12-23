using System;

namespace SERadioTorch
{
    /// <summary>
    /// Basic configuration for the Space Engineers Radio Torch plugin.
    /// </summary>
    public class RadioPluginConfig
    {
        /// <summary>
        /// UDP host/IP of the SE Radio server (matches server.py).
        /// </summary>
        public string ServerHost { get; set; } = "127.0.0.1";

        /// <summary>
        /// UDP port of the SE Radio server (matches server.py UDP_PORT).
        /// </summary>
        public int ServerPort { get; set; } = 8765;

        /// <summary>
        /// How often to push player position snapshots (milliseconds).
        /// </summary>
        public int UpdateIntervalMs { get; set; } = 5000;

        /// <summary>
        /// Toggle position forwarding without removing the plugin.
        /// </summary>
        public bool Enabled { get; set; } = true;

        /// <summary>
        /// Legacy identifier for this Torch/Nexus shard. Only used as a fallback when the
        /// instance-level RadioInstanceIdentity.json has not been populated yet.
        /// </summary>
        public string ServerTag { get; set; } = "default";

        public void Clamp()
        {
            if (UpdateIntervalMs < 100)
                UpdateIntervalMs = 100;
            if (UpdateIntervalMs > 60000)
                UpdateIntervalMs = 60000;
            ServerHost = string.IsNullOrWhiteSpace(ServerHost) ? "127.0.0.1" : ServerHost.Trim();
            ServerPort = Math.Max(1, Math.Min(65535, ServerPort));
            ServerTag = string.IsNullOrWhiteSpace(ServerTag) ? "default" : ServerTag.Trim();
        }
    }
}
