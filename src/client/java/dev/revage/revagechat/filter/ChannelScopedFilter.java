package dev.revage.revagechat.filter;

/**
 * Optional capability for per-channel enable/disable behavior.
 */
public interface ChannelScopedFilter {
    boolean isEnabledForChannel(String channelId);

    void setEnabledForChannel(String channelId, boolean enabled);
}
