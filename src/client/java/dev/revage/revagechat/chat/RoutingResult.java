package dev.revage.revagechat.chat;

import java.util.List;

/**
 * Routing output for a processed message.
 */
public final class RoutingResult {
    private List<ChatChannel> targetChannels;
    private boolean hideFromDefaultChat;

    public List<ChatChannel> targetChannels() {
        return targetChannels;
    }

    public boolean hideFromDefaultChat() {
        return hideFromDefaultChat;
    }

    public void set(List<ChatChannel> targetChannels, boolean hideFromDefaultChat) {
        this.targetChannels = targetChannels;
        this.hideFromDefaultChat = hideFromDefaultChat;
    }
}
