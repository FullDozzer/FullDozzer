package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;

public final class AddTagFilter extends AbstractChannelScopedFilter {
    private final String tag;

    public AddTagFilter(String tag) {
        this.tag = tag;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        return true;
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.setTag(ctx, tag);
    }
}
