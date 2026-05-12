package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Channel -> modular filter list registry.
 */
public final class ChannelFilterRegistry {
    private final Map<String, List<ChatFilter>> filtersByChannel = new ConcurrentHashMap<>();

    public void register(String channelId, ChatFilter filter) {
        filtersByChannel.computeIfAbsent(channelId, key -> new ArrayList<>(8)).add(filter);
    }

    public void clear(String channelId) {
        filtersByChannel.remove(channelId);
    }


    public List<ChatFilter> filters(String channelId) {
        List<ChatFilter> filters = activeFilters(channelId);
        return filters == null ? List.of() : List.copyOf(filters);
    }

    public void setEnabled(String channelId, ChatFilter filter, boolean enabled) {
        if (filter instanceof ChannelScopedFilter scoped) {
            scoped.setEnabledForChannel(channelId, enabled);
        }
    }

    private List<ChatFilter> activeFilters(String channelId) {
        List<ChatFilter> filters = filtersByChannel.get(channelId);
        if (filters == null || filters.isEmpty()) {
            filters = filtersByChannel.get("default");
        }
        return filters;
    }

    public boolean apply(String channelId, MessageContext context) {
        List<ChatFilter> filters = activeFilters(channelId);
        if (filters == null || filters.isEmpty()) {
            return true;
        }

        for (int i = 0, size = filters.size(); i < size; i++) {
            ChatFilter filter = filters.get(i);
            if (filter instanceof ChannelScopedFilter scoped && !scoped.isEnabledForChannel(channelId)) {
                continue;
            }

            if (filter.matches(context)) {
                filter.apply(context);
                if (FilterActionStore.isBlocked(context)) {
                    return false;
                }
            }
        }

        return true;
    }
}
