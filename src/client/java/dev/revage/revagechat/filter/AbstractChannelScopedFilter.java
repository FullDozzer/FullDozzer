package dev.revage.revagechat.filter;

import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Shared per-channel enable/disable mechanics.
 */
public abstract class AbstractChannelScopedFilter implements ChatFilter, ChannelScopedFilter {
    private final Set<String> disabledChannels = ConcurrentHashMap.newKeySet();

    @Override
    public boolean isEnabledForChannel(String channelId) {
        return !disabledChannels.contains(channelId);
    }

    @Override
    public void setEnabledForChannel(String channelId, boolean enabled) {
        if (enabled) {
            disabledChannels.remove(channelId);
        } else {
            disabledChannels.add(channelId);
        }
    }
}
