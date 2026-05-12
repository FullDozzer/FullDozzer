package dev.revage.revagechat.chat.model;

import dev.revage.revagechat.chat.MessageType;
import java.time.Instant;
import java.util.UUID;
import org.jetbrains.annotations.Nullable;

/**
 * Immutable message payload passed through the chat pipeline.
 */
public record MessageContext(
    String originalText,
    String formattedText,
    String senderName,
    @Nullable UUID uuid,
    Instant timestamp,
    MessageType messageType
) {
}
