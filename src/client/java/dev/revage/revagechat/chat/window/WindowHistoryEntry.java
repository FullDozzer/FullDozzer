package dev.revage.revagechat.chat.window;

import java.time.Instant;

/**
 * Immutable line rendered by a channel window.
 */
public record WindowHistoryEntry(String text, Instant timestamp) {
}
