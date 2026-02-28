package dev.revage.revagechat.chat;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.Deque;
import java.util.List;

/**
 * In-memory state for a single logical chat channel.
 */
public final class ChatChannel {
    private static final int MAX_HISTORY = 200;

    private final String id;
    private String name;
    private int color;
    private boolean customColor;
    private float opacity;
    private float volume;
    private final List<ChatFilter> filters;
    private WindowState windowState;
    private final Deque<MessageContext> messageHistory;

    public ChatChannel(String id, String name, int color, float opacity, float volume, WindowState windowState) {
        this.id = id;
        this.name = name;
        this.color = color;
        this.customColor = false;
        this.opacity = opacity;
        this.volume = volume;
        this.windowState = windowState;
        this.filters = new ArrayList<>(2);
        this.messageHistory = new ArrayDeque<>(MAX_HISTORY);
    }

    public String id() {
        return id;
    }

    public String name() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public int color() {
        return color;
    }

    public void setColor(int color) {
        this.color = color;
        this.customColor = true;
    }

    public boolean hasCustomColor() {
        return customColor;
    }

    public void clearCustomColor() {
        this.customColor = false;
    }

    public float opacity() {
        return opacity;
    }

    public void setOpacity(float opacity) {
        this.opacity = opacity;
    }

    public float volume() {
        return volume;
    }

    public void setVolume(float volume) {
        this.volume = volume;
    }

    public WindowState windowState() {
        return windowState;
    }

    public void setWindowState(WindowState windowState) {
        this.windowState = windowState;
    }

    public List<ChatFilter> filters() {
        return filters;
    }

    public Collection<MessageContext> messageHistory() {
        return Collections.unmodifiableCollection(messageHistory);
    }

    public void appendHistory(MessageContext context) {
        if (messageHistory.size() >= MAX_HISTORY) {
            messageHistory.removeFirst();
        }

        messageHistory.addLast(context);
    }
}
