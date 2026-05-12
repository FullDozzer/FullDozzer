package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.MessageType;
import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * Encapsulates filtering and moderation policies for messages.
 */
public final class FilterEngine {
    private final ChannelFilterRegistry registry;
    private final Map<String, ChatFilter> namedDefaults;
    private volatile boolean enabled;

    public FilterEngine() {
        this.registry = new ChannelFilterRegistry();
        this.namedDefaults = new LinkedHashMap<>();
        this.enabled = true;

        bootstrapDefaults();
    }

    public boolean allow(MessageContext context) {
        return enabled && !FilterActionStore.isBlocked(context);
    }

    public boolean apply(String channelId, MessageContext context) {
        if (!enabled) {
            return true;
        }

        return registry.apply(channelId, context);
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public boolean isEnabled() {
        return enabled;
    }

    public ChannelFilterRegistry registry() {
        return registry;
    }

    public Set<String> defaultFilterIds() {
        return namedDefaults.keySet();
    }

    public boolean isFilterEnabled(String channelId, String filterId) {
        ChatFilter filter = namedDefaults.get(filterId);
        if (filter instanceof ChannelScopedFilter scoped) {
            return scoped.isEnabledForChannel(channelId);
        }
        return false;
    }

    public void setFilterEnabled(String channelId, String filterId, boolean enabled) {
        ChatFilter filter = namedDefaults.get(filterId);
        if (filter == null) {
            return;
        }
        registry.setEnabled(channelId, filter, enabled);
    }

    public void applyPersistedStates(Map<String, Map<String, Boolean>> states) {
        if (states == null || states.isEmpty()) {
            return;
        }

        for (Map.Entry<String, Map<String, Boolean>> channelEntry : states.entrySet()) {
            String channelId = channelEntry.getKey();
            Map<String, Boolean> filterStates = channelEntry.getValue();
            if (filterStates == null) {
                continue;
            }

            for (Map.Entry<String, Boolean> filterEntry : filterStates.entrySet()) {
                setFilterEnabled(channelId, filterEntry.getKey(), Boolean.TRUE.equals(filterEntry.getValue()));
            }
        }
    }

    public Map<String, Map<String, Boolean>> snapshotStates(Collection<String> channelIds) {
        Map<String, Map<String, Boolean>> states = new LinkedHashMap<>();
        for (String channelId : channelIds) {
            Map<String, Boolean> oneChannel = new LinkedHashMap<>();
            for (String filterId : namedDefaults.keySet()) {
                oneChannel.put(filterId, isFilterEnabled(channelId, filterId));
            }
            states.put(channelId, oneChannel);
        }
        return states;
    }

    private void bootstrapDefaults() {
        addDefault("anti_caps", new AntiCapsFilter(0.80F));
        addDefault("duplicate_merge", new DuplicateMergeFilter());
        addDefault("length", new LengthFilter(1, 400));
        addDefault("hide_system", new MessageTypeFilter(Set.of(MessageType.SYSTEM)));
    }

    private void addDefault(String id, ChatFilter filter) {
        namedDefaults.put(id, filter);
        registry.register("default", filter);
    }
}
