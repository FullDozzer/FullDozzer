package dev.revage.revagechat.log;

import dev.revage.revagechat.RevageChatClient;
import dev.revage.revagechat.chat.model.MessageContext;

/**
 * Provides centralized logging hooks for chat message events.
 */
public final class LogManager {

    public void appendIncoming(MessageContext context, String channel) {
        // TODO: Replace with file/session logging once format is finalized.
        RevageChatClient.LOGGER.trace(
            "Incoming [{}|sender={}|type={}] {}",
            channel,
            context.senderName(),
            context.messageType(),
            context.formattedText()
        );
    }

    public void appendOutgoing(MessageContext context, String channel) {
        // TODO: Replace with file/session logging once format is finalized.
        RevageChatClient.LOGGER.trace(
            "Outgoing [{}|type={}] {}",
            channel,
            context.messageType(),
            context.formattedText()
        );
    }
}
