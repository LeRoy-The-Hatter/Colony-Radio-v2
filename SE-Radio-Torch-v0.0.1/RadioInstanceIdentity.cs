using System;

namespace SERadioTorch
{
    /// <summary>
    /// Identity stored in the Torch instance directory so multiple shards can be
    /// distinguished by name and SSRC.
    /// </summary>
    public class RadioInstanceIdentity
    {
        /// <summary>
        /// Human-friendly name for this Torch server/shard.
        /// </summary>
        public string ServerName { get; set; }

        /// <summary>
        /// Unique SSRC allocated for this server; used as a salt when deriving
        /// per-player SSRCs so identical SteamIDs on different shards do not collide.
        /// </summary>
        public uint ServerSsrc { get; set; }

        public void Clamp(string fallbackName, Func<string, uint> ssrcFactory)
        {
            var name = string.IsNullOrWhiteSpace(ServerName) ? fallbackName : ServerName;
            ServerName = string.IsNullOrWhiteSpace(name) ? "default" : name.Trim();

            if (ServerSsrc == 0)
            {
                try
                {
                    ServerSsrc = ssrcFactory != null ? ssrcFactory(ServerName) : 1u;
                }
                catch
                {
                    ServerSsrc = 1;
                }
            }

            if (ServerSsrc == 0)
                ServerSsrc = 1;
        }
    }
}
