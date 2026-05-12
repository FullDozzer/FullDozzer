package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.regex.Pattern;

public final class ReplaceTextFilter extends AbstractChannelScopedFilter {
    private final Pattern pattern;
    private final String replacement;

    public ReplaceTextFilter(String regex, String replacement) {
        this.pattern = Pattern.compile(regex);
        this.replacement = replacement;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        return pattern.matcher(FilterActionStore.text(ctx)).find();
    }

    @Override
    public void apply(MessageContext ctx) {
        String source = FilterActionStore.text(ctx);
        FilterActionStore.setText(ctx, pattern.matcher(source).replaceAll(replacement));
    }
}
