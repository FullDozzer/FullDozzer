package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Locale;

public final class AntiCapsFilter extends AbstractChannelScopedFilter {
    private final float threshold;

    public AntiCapsFilter(float threshold) {
        this.threshold = threshold;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        String text = FilterActionStore.text(ctx);
        int letters = 0;
        int upper = 0;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (Character.isLetter(c)) {
                letters++;
                if (Character.isUpperCase(c)) {
                    upper++;
                }
            }
        }

        return letters > 4 && (upper / (float) letters) >= threshold;
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.setText(ctx, FilterActionStore.text(ctx).toLowerCase(Locale.ROOT));
    }
}
