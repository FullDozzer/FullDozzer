package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public final class DuplicateMergeFilter extends AbstractChannelScopedFilter {
    private final Map<String, DupState> bySender = new ConcurrentHashMap<>();

    @Override
    public boolean matches(MessageContext ctx) {
        DupState state = bySender.get(ctx.senderName());
        if (state == null) {
            return false;
        }

        return state.lastText.equals(FilterActionStore.text(ctx));
    }

    @Override
    public void apply(MessageContext ctx) {
        DupState state = bySender.computeIfAbsent(ctx.senderName(), ignored -> new DupState());
        String text = FilterActionStore.text(ctx);

        if (text.equals(state.lastText)) {
            state.count++;
            FilterActionStore.setText(ctx, text + " x" + state.count);
        } else {
            state.lastText = text;
            state.count = 1;
        }
    }

    private static final class DupState {
        private String lastText = "";
        private int count = 0;
    }
}
