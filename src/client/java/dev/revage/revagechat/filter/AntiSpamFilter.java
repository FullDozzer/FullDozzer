package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public final class AntiSpamFilter extends AbstractChannelScopedFilter {
    private final int maxMessages;
    private final long windowMillis;
    private final Map<String, ArrayDeque<Long>> bySender = new ConcurrentHashMap<>();

    public AntiSpamFilter(int maxMessages, long windowMillis) {
        this.maxMessages = maxMessages;
        this.windowMillis = windowMillis;
    }

    @Override
    public boolean matches(MessageContext ctx) {
        String sender = ctx.senderName();
        long now = millis(ctx.timestamp());

        ArrayDeque<Long> queue = bySender.computeIfAbsent(sender, ignored -> new ArrayDeque<>(maxMessages + 2));
        while (!queue.isEmpty() && now - queue.peekFirst() > windowMillis) {
            queue.removeFirst();
        }

        return queue.size() >= maxMessages;
    }

    @Override
    public void apply(MessageContext ctx) {
        String sender = ctx.senderName();
        long now = millis(ctx.timestamp());
        bySender.computeIfAbsent(sender, ignored -> new ArrayDeque<>(maxMessages + 2)).addLast(now);
        FilterActionStore.block(ctx);
    }

    private long millis(Instant instant) {
        return instant.toEpochMilli();
    }
}
