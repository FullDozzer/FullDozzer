package dev.revage.revagechat.chat.window;

import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gui.DrawContext;

/**
 * Manages independent channel windows and shared input routing.
 */
public final class ChannelWindowManager {
    private final Map<String, ChannelWindow> windowsByChannelId;

    public ChannelWindowManager() {
        this.windowsByChannelId = new LinkedHashMap<>();
    }

    public ChannelWindow getOrCreate(String channelId, int x, int y, int width, int height) {
        return windowsByChannelId.computeIfAbsent(channelId, ignored -> new ChannelWindow(channelId, x, y, width, height));
    }

    public ChannelWindow getOrCreateDefault(String channelId) {
        int index = Math.max(0, windowsByChannelId.size());
        return getOrCreate(channelId, 10 + (index * 14), 20 + (index * 14), 280, 150);
    }

    public Optional<ChannelWindow> get(String channelId) {
        return Optional.ofNullable(windowsByChannelId.get(channelId));
    }

    public void appendMessage(String channelId, String text) {
        getOrCreateDefault(channelId).addMessage(text);
    }

    public void remove(String channelId) {
        ChannelWindow removed = windowsByChannelId.remove(channelId);
        if (removed != null) {
            removed.clear();
        }
    }

    public Collection<ChannelWindow> windows() {
        return Collections.unmodifiableCollection(windowsByChannelId.values());
    }

    public void tick(MinecraftClient client, float deltaTime) {
        int screenWidth = client.getWindow().getScaledWidth();
        int screenHeight = client.getWindow().getScaledHeight();

        for (ChannelWindow window : windowsByChannelId.values()) {
            window.tick(deltaTime, screenWidth, screenHeight);
        }
    }

    public void renderAll(DrawContext context, MinecraftClient client) {
        for (ChannelWindow window : windowsByChannelId.values()) {
            window.render(context, client);
        }
    }

    public boolean onMouseDown(double mouseX, double mouseY, int button) {
        ChannelWindow[] ordered = windowsByChannelId.values().toArray(new ChannelWindow[0]);
        for (int i = ordered.length - 1; i >= 0; i--) {
            if (ordered[i].onMouseDown(mouseX, mouseY, button)) {
                return true;
            }
        }

        return false;
    }

    public boolean onMouseDrag(double mouseX, double mouseY, int button) {
        for (ChannelWindow window : windowsByChannelId.values()) {
            if (window.onMouseDrag(mouseX, mouseY, button)) {
                return true;
            }
        }

        return false;
    }

    public boolean onMouseUp(int button) {
        for (ChannelWindow window : windowsByChannelId.values()) {
            if (window.onMouseUp(button)) {
                return true;
            }
        }

        return false;
    }

    public boolean onMouseScroll(double mouseX, double mouseY, double amount) {
        for (ChannelWindow window : windowsByChannelId.values()) {
            if (window.onScroll(mouseX, mouseY, amount)) {
                return true;
            }
        }

        return false;
    }

    public void clear() {
        for (ChannelWindow window : windowsByChannelId.values()) {
            window.clear();
        }
        windowsByChannelId.clear();
    }
}
