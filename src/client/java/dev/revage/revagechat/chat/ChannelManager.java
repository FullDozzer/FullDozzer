package dev.revage.revagechat.chat;

/**
 * Maintains channel-level state and routing decisions.
 */
public final class ChannelManager {

    public String resolveChannel(String rawMessage) {
        // TODO: Identify channels by prefix/syntax and player context.
        return "default";
    }
}
