package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;

public final class RecolorFilter extends AbstractChannelScopedFilter {
    private final int rgb;

    public RecolorFilter(int rgb) {
        this.rgb = rgb;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        return !FilterActionStore.isBlocked(ctx);
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.setRecolor(ctx, rgb);
    }
}
