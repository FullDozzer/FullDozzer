package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.MessageType;
import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Set;

/**
 * Encapsulates filtering and moderation policies for messages.
 */
public final class FilterEngine {
    private final ChannelFilterRegistry registry;
    private volatile boolean enabled;

    public FilterEngine() {
        this.registry = new ChannelFilterRegistry();
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

    private void bootstrapDefaults() {
        // conservative defaults for ready-to-use experience
        registry.register("default", new AntiCapsFilter(0.80F));
        registry.register("default", new DuplicateMergeFilter());
        registry.register("default", new LengthFilter(1, 400));
        registry.register("default", new MessageTypeFilter(Set.of(MessageType.SYSTEM)));
    }
}
