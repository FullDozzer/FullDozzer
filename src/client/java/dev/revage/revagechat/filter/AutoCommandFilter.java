package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Locale;

public final class AutoCommandFilter extends AbstractChannelScopedFilter {
    private final String trigger;
    private final String command;

    public AutoCommandFilter(String trigger, String command) {
        this.trigger = trigger.toLowerCase(Locale.ROOT);
        this.command = command;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        return FilterActionStore.text(ctx).toLowerCase(Locale.ROOT).contains(trigger);
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.setAutoCommand(ctx, command);
    }
}
