package dev.revage.revagechat.chat;

import dev.revage.revagechat.chat.model.MessageContext;

/**
 * Applies lightweight text transformation for a routed message.
 */
@FunctionalInterface
public interface ChatTransformer {
    String transform(MessageContext context, ChatChannel channel, String currentFormattedText);
}
