package dev.revage.revagechat.chat;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.List;

/**
 * Side-effect hook executed after routing.
 */
@FunctionalInterface
public interface ChatPostAction {
    void run(MessageContext context, List<ChatChannel> channels, boolean hideFromDefaultChat);
}
