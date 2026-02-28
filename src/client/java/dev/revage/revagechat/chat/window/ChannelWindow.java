package dev.revage.revagechat.chat.window;

import java.time.Instant;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.font.TextRenderer;
import net.minecraft.client.gui.DrawContext;
import net.minecraft.text.Text;

/**
 * Independent draggable chat window bound to a channel id.
 */
public final class ChannelWindow {
    private static final int MIN_WIDTH = 90;
    private static final int MIN_HEIGHT = 60;
    private static final int HEADER_HEIGHT = 12;
    private static final int RESIZE_HANDLE = 8;
    private static final int SNAP_MARGIN = 8;
    private static final int MAX_HISTORY = 300;
    private static final float INERTIA_DAMPING = 0.86F;
    private static final float FADE_SPEED = 0.11F;

    private final String channelId;
    private final Deque<WindowHistoryEntry> history;
    private final ArrayList<WindowHistoryEntry> cachedVisibleLines;

    private int x;
    private int y;
    private int width;
    private int height;
    private boolean draggable;
    private boolean resizable;
    private boolean minimized;
    private float opacity;
    private WindowAnimationState animationState;

    private boolean dragging;
    private boolean resizing;
    private int dragOffsetX;
    private int dragOffsetY;
    private int resizeOriginX;
    private int resizeOriginY;
    private int resizeStartWidth;
    private int resizeStartHeight;

    private float scrollOffset;
    private float scrollVelocity;

    private int cachedLayoutWidth = -1;
    private int cachedLayoutHeight = -1;
    private int cachedVisibleCount;

    public ChannelWindow(String channelId, int x, int y, int width, int height) {
        this.channelId = channelId;
        this.x = x;
        this.y = y;
        this.width = Math.max(width, MIN_WIDTH);
        this.height = Math.max(height, MIN_HEIGHT);
        this.draggable = true;
        this.resizable = true;
        this.minimized = false;
        this.opacity = 0.85F;
        this.animationState = WindowAnimationState.FADING_IN;
        this.history = new ArrayDeque<>(MAX_HISTORY);
        this.cachedVisibleLines = new ArrayList<>(64);
    }

    public String channelId() {
        return channelId;
    }

    public int x() {
        return x;
    }

    public int y() {
        return y;
    }

    public int width() {
        return width;
    }

    public int height() {
        return height;
    }

    public void setOpacity(float opacity) {
        this.opacity = Math.max(0.05F, Math.min(1.0F, opacity));
    }

    public float opacity() {
        return opacity;
    }

    public void setMinimized(boolean minimized) {
        this.minimized = minimized;
        invalidateLayout();
    }

    public boolean minimized() {
        return minimized;
    }

    public void setBounds(int x, int y, int width, int height) {
        this.x = x;
        this.y = y;
        this.width = Math.max(width, MIN_WIDTH);
        this.height = Math.max(height, MIN_HEIGHT);
        invalidateLayout();
    }

    public void setDraggable(boolean draggable) {
        this.draggable = draggable;
    }

    public void setResizable(boolean resizable) {
        this.resizable = resizable;
    }

    public WindowAnimationState animationState() {
        return animationState;
    }

    public void addMessage(String text) {
        if (history.size() >= MAX_HISTORY) {
            history.removeFirst();
        }

        history.addLast(new WindowHistoryEntry(text, Instant.now()));
        invalidateLayout();
    }

    public void tick(float deltaTime, int screenWidth, int screenHeight) {
        updateAnimation();
        updateScrollInertia();

        if (!dragging && !resizing) {
            snapToEdges(screenWidth, screenHeight);
        }

        clampToScreen(screenWidth, screenHeight);
    }

    public void render(DrawContext context, MinecraftClient client) {
        int bgAlpha = (int) (160 * effectiveAlpha());
        int borderAlpha = (int) (220 * effectiveAlpha());
        int textAlpha = (int) (255 * effectiveAlpha());

        context.fill(x, y, x + width, y + HEADER_HEIGHT, (bgAlpha << 24) | 0x2B2B2B);
        context.fill(x, y + HEADER_HEIGHT, x + width, y + (minimized ? HEADER_HEIGHT + 1 : height), (bgAlpha << 24) | 0x111111);

        context.drawBorder(x, y, width, minimized ? HEADER_HEIGHT + 1 : height, (borderAlpha << 24) | 0x777777);

        TextRenderer renderer = client.textRenderer;
        context.drawText(renderer, Text.literal("#" + channelId), x + 4, y + 2, (textAlpha << 24) | 0xFFFFFF, false);

        if (minimized) {
            return;
        }

        if (resizable) {
            context.fill(
                x + width - RESIZE_HANDLE,
                y + height - RESIZE_HANDLE,
                x + width,
                y + height,
                (borderAlpha << 24) | 0x999999
            );
        }

        rebuildVisibleCacheIfNeeded();

        int lineHeight = 9;
        int baseY = y + HEADER_HEIGHT + 3;
        int maxLines = Math.max(0, (height - HEADER_HEIGHT - 6) / lineHeight);
        int start = Math.min((int) scrollOffset, Math.max(0, cachedVisibleCount - maxLines));

        for (int i = 0; i < maxLines && start + i < cachedVisibleCount; i++) {
            WindowHistoryEntry line = cachedVisibleLines.get(start + i);
            context.drawText(renderer, line.text(), x + 4, baseY + i * lineHeight, (textAlpha << 24) | 0xDDDDDD, false);
        }
    }

    public boolean onMouseDown(double mouseX, double mouseY, int button) {
        if (button != 0 || !contains(mouseX, mouseY)) {
            return false;
        }

        if (resizable && isOnResizeCorner(mouseX, mouseY)) {
            resizing = true;
            resizeOriginX = (int) mouseX;
            resizeOriginY = (int) mouseY;
            resizeStartWidth = width;
            resizeStartHeight = height;
            return true;
        }

        if (draggable && isOnHeader(mouseX, mouseY)) {
            dragging = true;
            dragOffsetX = (int) mouseX - x;
            dragOffsetY = (int) mouseY - y;
            return true;
        }

        return false;
    }

    public boolean onMouseDrag(double mouseX, double mouseY, int button) {
        if (button != 0) {
            return false;
        }

        if (dragging) {
            x = (int) mouseX - dragOffsetX;
            y = (int) mouseY - dragOffsetY;
            return true;
        }

        if (resizing) {
            int deltaX = (int) mouseX - resizeOriginX;
            int deltaY = (int) mouseY - resizeOriginY;
            width = Math.max(MIN_WIDTH, resizeStartWidth + deltaX);
            height = Math.max(MIN_HEIGHT, resizeStartHeight + deltaY);
            invalidateLayout();
            return true;
        }

        return false;
    }

    public boolean onMouseUp(int button) {
        if (button != 0) {
            return false;
        }

        boolean handled = dragging || resizing;
        dragging = false;
        resizing = false;
        return handled;
    }

    public boolean onScroll(double mouseX, double mouseY, double amount) {
        if (!contains(mouseX, mouseY) || minimized) {
            return false;
        }

        scrollVelocity += (float) (-amount * 2.4F);
        return true;
    }

    public void clear() {
        history.clear();
        cachedVisibleLines.clear();
        cachedVisibleCount = 0;
    }

    private void updateAnimation() {
        if (animationState != WindowAnimationState.FADING_IN) {
            return;
        }

        opacity += FADE_SPEED;
        if (opacity >= 1.0F) {
            opacity = 1.0F;
            animationState = WindowAnimationState.VISIBLE;
        }
    }

    private void updateScrollInertia() {
        if (Math.abs(scrollVelocity) < 0.01F) {
            scrollVelocity = 0.0F;
            return;
        }

        scrollOffset += scrollVelocity;
        scrollVelocity *= INERTIA_DAMPING;

        int maxScroll = Math.max(0, cachedVisibleCount - visibleLineCapacity());
        if (scrollOffset < 0) {
            scrollOffset = 0;
            scrollVelocity = 0;
        } else if (scrollOffset > maxScroll) {
            scrollOffset = maxScroll;
            scrollVelocity = 0;
        }
    }

    private int visibleLineCapacity() {
        if (minimized) {
            return 0;
        }

        return Math.max(0, (height - HEADER_HEIGHT - 6) / 9);
    }

    private void rebuildVisibleCacheIfNeeded() {
        if (cachedLayoutWidth == width && cachedLayoutHeight == height && cachedVisibleCount == history.size()) {
            return;
        }

        cachedVisibleLines.clear();
        cachedVisibleLines.ensureCapacity(history.size());
        cachedVisibleLines.addAll(history);

        cachedLayoutWidth = width;
        cachedLayoutHeight = height;
        cachedVisibleCount = cachedVisibleLines.size();

        int maxScroll = Math.max(0, cachedVisibleCount - visibleLineCapacity());
        if (scrollOffset > maxScroll) {
            scrollOffset = maxScroll;
        }
    }

    private void invalidateLayout() {
        cachedLayoutWidth = -1;
    }

    private boolean contains(double mouseX, double mouseY) {
        int effectiveHeight = minimized ? HEADER_HEIGHT + 1 : height;
        return mouseX >= x && mouseX <= x + width && mouseY >= y && mouseY <= y + effectiveHeight;
    }

    private boolean isOnHeader(double mouseX, double mouseY) {
        return mouseX >= x && mouseX <= x + width && mouseY >= y && mouseY <= y + HEADER_HEIGHT;
    }

    private boolean isOnResizeCorner(double mouseX, double mouseY) {
        return mouseX >= x + width - RESIZE_HANDLE
            && mouseX <= x + width
            && mouseY >= y + height - RESIZE_HANDLE
            && mouseY <= y + height;
    }

    private void snapToEdges(int screenWidth, int screenHeight) {
        if (Math.abs(x) <= SNAP_MARGIN) {
            x = 0;
        }

        if (Math.abs(y) <= SNAP_MARGIN) {
            y = 0;
        }

        int rightGap = screenWidth - (x + width);
        if (Math.abs(rightGap) <= SNAP_MARGIN) {
            x = screenWidth - width;
        }

        int bottomGap = screenHeight - (y + (minimized ? HEADER_HEIGHT + 1 : height));
        if (Math.abs(bottomGap) <= SNAP_MARGIN) {
            y = screenHeight - (minimized ? HEADER_HEIGHT + 1 : height);
        }
    }

    private void clampToScreen(int screenWidth, int screenHeight) {
        int effectiveHeight = minimized ? HEADER_HEIGHT + 1 : height;

        if (x < 0) {
            x = 0;
        }
        if (y < 0) {
            y = 0;
        }
        if (x + width > screenWidth) {
            x = Math.max(0, screenWidth - width);
        }
        if (y + effectiveHeight > screenHeight) {
            y = Math.max(0, screenHeight - effectiveHeight);
        }
    }

    private float effectiveAlpha() {
        return switch (animationState) {
            case HIDDEN -> 0.0F;
            case FADING_IN, VISIBLE -> opacity;
        };
    }
}
