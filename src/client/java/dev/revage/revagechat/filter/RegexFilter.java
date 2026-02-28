package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.regex.Pattern;

public final class RegexFilter extends AbstractChannelScopedFilter {
    private final Pattern pattern;

    public RegexFilter(String regex) {
        this.pattern = Pattern.compile(regex, Pattern.CASE_INSENSITIVE);
    }

    @Override
    public boolean matches(MessageContext ctx) {
        return pattern.matcher(FilterActionStore.text(ctx)).find();
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.block(ctx);
    }
}
