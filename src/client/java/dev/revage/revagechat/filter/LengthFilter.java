package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;

public final class LengthFilter extends AbstractChannelScopedFilter {
    private final int min;
    private final int max;

    public LengthFilter(int min, int max) {
        this.min = min;
        this.max = max;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        int len = FilterActionStore.text(ctx).length();
        return len < min || len > max;
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.block(ctx);
    }
}
