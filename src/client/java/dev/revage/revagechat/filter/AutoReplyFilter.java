package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Locale;

public final class AutoReplyFilter extends AbstractChannelScopedFilter {
    private final String trigger;
    private final String reply;

    public AutoReplyFilter(String trigger, String reply) {
        this.trigger = trigger.toLowerCase(Locale.ROOT);
        this.reply = reply;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        return FilterActionStore.text(ctx).toLowerCase(Locale.ROOT).contains(trigger);
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.setAutoReply(ctx, reply);
    }
}
