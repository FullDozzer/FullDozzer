package dev.revage.revagechat.chat;

import dev.revage.revagechat.chat.model.MessageContext;

/**
 * Predicate-style filter for chat pipeline processing.
 */
@FunctionalInterface
public interface ChatFilter {
    boolean allow(MessageContext context, ChatChannel channel);
}
