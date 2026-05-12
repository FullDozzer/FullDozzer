package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Locale;
import java.util.Set;

public final class HighlightWordFilter extends AbstractChannelScopedFilter {
    private final Set<String> highlights;

    public HighlightWordFilter(Set<String> highlights) {
        this.highlights = highlights;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        String text = FilterActionStore.text(ctx).toLowerCase(Locale.ROOT);
        for (String word : highlights) {
            if (text.contains(word.toLowerCase(Locale.ROOT))) {
                return true;
            }
        }
        return false;
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.setTag(ctx, "highlight");
    }
}
