package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;

/**
 * Encapsulates filtering and moderation policies for messages.
 */
public final class FilterEngine {

    public boolean allow(MessageContext context) {
        // TODO: Evaluate configured filter rules against message content and message type.
        return true;
    }
}
