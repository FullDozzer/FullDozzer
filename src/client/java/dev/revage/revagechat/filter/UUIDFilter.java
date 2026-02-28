package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Set;
import java.util.UUID;

public final class UUIDFilter extends AbstractChannelScopedFilter {
    private final Set<UUID> blocked;

    public UUIDFilter(Set<UUID> blocked) {
        this.blocked = blocked;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        return ctx.uuid() != null && blocked.contains(ctx.uuid());
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.block(ctx);
    }
}
