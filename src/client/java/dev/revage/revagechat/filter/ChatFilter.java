package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;

/**
 * Core modular filter contract.
 */
public interface ChatFilter {
    boolean matches(MessageContext ctx);

    void apply(MessageContext ctx);
}
