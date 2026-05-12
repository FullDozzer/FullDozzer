package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.MessageType;
import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Set;

public final class MessageTypeFilter extends AbstractChannelScopedFilter {
    private final Set<MessageType> blockedTypes;

    public MessageTypeFilter(Set<MessageType> blockedTypes) {
        this.blockedTypes = blockedTypes;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        return blockedTypes.contains(ctx.messageType());
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.block(ctx);
    }
}
