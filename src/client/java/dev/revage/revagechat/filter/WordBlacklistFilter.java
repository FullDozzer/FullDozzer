package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

public final class WordBlacklistFilter extends AbstractChannelScopedFilter {
    private final Set<String> blockedWords;

    public WordBlacklistFilter(Set<String> blockedWords) {
        this.blockedWords = new HashSet<>(blockedWords.size());
        for (String word : blockedWords) {
            this.blockedWords.add(word.toLowerCase(Locale.ROOT));
        }
    }

    @Override
    public boolean matches(MessageContext ctx) {
        String text = FilterActionStore.text(ctx).toLowerCase(Locale.ROOT);
        for (String blocked : blockedWords) {
            if (text.contains(blocked)) {
                return true;
            }
        }
        return false;
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.block(ctx);
    }
}
