package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

public final class WordWhitelistFilter extends AbstractChannelScopedFilter {
    private final Set<String> allowedWords;

    public WordWhitelistFilter(Set<String> allowedWords) {
        this.allowedWords = new HashSet<>(allowedWords.size());
        for (String word : allowedWords) {
            this.allowedWords.add(word.toLowerCase(Locale.ROOT));
        }
    }

    @Override
    public boolean matches(MessageContext ctx) {
        String text = FilterActionStore.text(ctx);
        int start = -1;

        for (int i = 0; i <= text.length(); i++) {
            boolean boundary = i == text.length() || text.charAt(i) == ' ';
            if (boundary) {
                if (start >= 0) {
                    String token = text.substring(start, i).toLowerCase(Locale.ROOT);
                    if (!allowedWords.contains(token)) {
                        return true;
                    }
                    start = -1;
                }
            } else if (start < 0) {
                start = i;
            }
        }

        return false;
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.block(ctx);
    }
}
