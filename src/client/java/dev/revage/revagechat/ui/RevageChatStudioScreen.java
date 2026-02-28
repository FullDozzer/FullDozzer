package dev.revage.revagechat.ui;

import dev.revage.revagechat.RevageChatClient;
import dev.revage.revagechat.chat.ChatChannel;
import dev.revage.revagechat.chat.WindowState;
import dev.revage.revagechat.chat.window.ChannelWindow;
import dev.revage.revagechat.chat.window.ChannelWindowManager;
import dev.revage.revagechat.config.ConfigManager;
import dev.revage.revagechat.filter.FilterEngine;
import java.util.ArrayList;
import java.util.List;
import net.minecraft.client.gui.DrawContext;
import net.minecraft.client.gui.screen.Screen;
import net.minecraft.client.gui.widget.ButtonWidget;
import net.minecraft.client.gui.widget.SliderWidget;
import net.minecraft.client.gui.widget.TextFieldWidget;
import net.minecraft.text.Text;

/**
 * Professional control center for channels, filters and windows.
 */
public final class RevageChatStudioScreen extends Screen {
    private static final int BG = 0xFF202225;
    private static final int PANEL = 0xFF111317;
    private static final int SIDEBAR = 0xFF17191F;
    private static final int ACCENT = 0xFF5865F2;
    private static final int MUTED = 0xFFB9BBBE;

    private final Screen parent;
    private final ConfigManager config;
    private final FilterEngine filters;
    private final ChannelWindowManager windows;
    private final Runnable onSave;

    private final List<String> channelIds;
    private int selectedChannel;

    private TextFieldWidget colorField;
    private TextFieldWidget channelNameField;
    private TextFieldWidget channelCreateField;
    private ButtonWidget customColorButton;

    public RevageChatStudioScreen(Screen parent, ConfigManager config, FilterEngine filters, ChannelWindowManager windows, Runnable onSave) {
        super(Text.literal("RevageChat Studio"));
        this.parent = parent;
        this.config = config;
        this.filters = filters;
        this.windows = windows;
        this.onSave = onSave;
        this.channelIds = new ArrayList<>();
    }

    @Override
    protected void init() {
        reloadChannels();

        int x = width / 2 - 260;
        int y = height / 2 - 160;

        addDrawableChild(ButtonWidget.builder(Text.literal("< Prev"), b -> shiftChannel(-1))
            .dimensions(x + 160, y + 38, 80, 20).build());
        addDrawableChild(ButtonWidget.builder(Text.literal("Next >"), b -> shiftChannel(1))
            .dimensions(x + 245, y + 38, 80, 20).build());

        channelNameField = new TextFieldWidget(textRenderer, x + 160, y + 66, 165, 20, Text.literal("Channel name"));
        channelNameField.setText(currentChannel().name());
        addDrawableChild(channelNameField);

        addDrawableChild(ButtonWidget.builder(Text.literal("Rename"), b -> renameCurrentChannel())
            .dimensions(x + 330, y + 66, 95, 20).build());

        colorField = new TextFieldWidget(textRenderer, x + 160, y + 92, 100, 20, Text.literal("#RRGGBB"));
        colorField.setText(currentChannelHex());
        addDrawableChild(colorField);

        customColorButton = addDrawableChild(ButtonWidget.builder(customColorText(), b -> toggleCustomColor())
            .dimensions(x + 265, y + 92, 160, 20).build());

        addDrawableChild(new PercentSlider(
            x + 160,
            y + 118,
            265,
            20,
            currentChannel().volume(),
            value -> currentChannel().setVolume((float) value),
            "Channel volume"
        ));

        addDrawableChild(new PercentSlider(
            x + 160,
            y + 142,
            265,
            20,
            currentChannel().opacity(),
            value -> {
                currentChannel().setOpacity((float) value);
                windows.getOrCreateDefault(currentChannel().id()).setOpacity((float) value);
            },
            "Window opacity"
        ));

        addDrawableChild(ButtonWidget.builder(minimizedText(), b -> toggleMinimized(b))
            .dimensions(x + 160, y + 166, 130, 20).build());

        addDrawableChild(ButtonWidget.builder(Text.literal("Open Window"), b -> windows.getOrCreateDefault(currentChannel().id()))
            .dimensions(x + 295, y + 166, 130, 20).build());

        int filterY = y + 194;
        for (String filterId : filters.defaultFilterIds()) {
            addDrawableChild(ButtonWidget.builder(filterButtonText(filterId), b -> toggleFilter(filterId, b))
                .dimensions(x + 160, filterY, 265, 20).build());
            filterY += 24;
        }

        channelCreateField = new TextFieldWidget(textRenderer, x + 16, y + 250, 110, 20, Text.literal("new-channel"));
        addDrawableChild(channelCreateField);

        addDrawableChild(ButtonWidget.builder(Text.literal("Create"), b -> createChannel())
            .dimensions(x + 16, y + 274, 110, 20).build());

        addDrawableChild(ButtonWidget.builder(Text.literal("Delete"), b -> deleteChannel())
            .dimensions(x + 16, y + 298, 110, 20).build());

        addDrawableChild(ButtonWidget.builder(Text.literal("Save"), b -> save())
            .dimensions(x + 316, y + 298, 110, 22).build());

        addDrawableChild(ButtonWidget.builder(Text.literal("Back"), b -> close())
            .dimensions(x + 200, y + 298, 110, 22).build());
    }

    @Override
    public void close() {
        if (client != null) {
            client.setScreen(parent);
        }
    }

    @Override
    public boolean mouseClicked(double mouseX, double mouseY, int button) {
        if (windows.onMouseDown(mouseX, mouseY, button)) {
            return true;
        }
        return super.mouseClicked(mouseX, mouseY, button);
    }

    @Override
    public boolean mouseDragged(double mouseX, double mouseY, int button, double deltaX, double deltaY) {
        if (windows.onMouseDrag(mouseX, mouseY, button)) {
            return true;
        }
        return super.mouseDragged(mouseX, mouseY, button, deltaX, deltaY);
    }

    @Override
    public boolean mouseReleased(double mouseX, double mouseY, int button) {
        if (windows.onMouseUp(button)) {
            return true;
        }
        return super.mouseReleased(mouseX, mouseY, button);
    }

    @Override
    public boolean mouseScrolled(double mouseX, double mouseY, double horizontalAmount, double verticalAmount) {
        if (windows.onMouseScroll(mouseX, mouseY, verticalAmount)) {
            return true;
        }
        return super.mouseScrolled(mouseX, mouseY, horizontalAmount, verticalAmount);
    }

    @Override
    public void render(DrawContext context, int mouseX, int mouseY, float delta) {
        int x = width / 2 - 260;
        int y = height / 2 - 160;

        context.fill(0, 0, width, height, BG);
        context.fill(x, y, x + 520, y + 330, PANEL);
        context.fill(x, y, x + 140, y + 330, SIDEBAR);
        context.fill(x, y, x + 520, y + 28, ACCENT);

        context.drawText(textRenderer, Text.literal("RevageChat Studio"), x + 8, y + 9, 0xFFFFFFFF, false);
        context.drawText(textRenderer, Text.literal("Channels"), x + 20, y + 44, MUTED, false);

        int cy = y + 66;
        for (int i = 0; i < channelIds.size() && i < 10; i++) {
            boolean selected = i == selectedChannel;
            int itemColor = selected ? 0xFF2F3136 : 0xFF1F2128;
            context.fill(x + 10, cy, x + 130, cy + 18, itemColor);
            context.drawText(textRenderer, Text.literal("#" + channelIds.get(i)), x + 15, cy + 5, selected ? 0xFFFFFFFF : MUTED, false);
            cy += 22;
        }

        context.drawText(textRenderer, Text.literal("Current channel: #" + currentChannel().id()), x + 160, y + 44, 0xFFE3E5E8, false);
        context.drawText(textRenderer, Text.literal("Name / color / sound / window"), x + 160, y + 56, MUTED, false);
        context.drawText(textRenderer, Text.literal("Filter toggles"), x + 160, y + 184, MUTED, false);
        context.drawText(textRenderer, Text.literal("You can drag and resize windows directly"), x + 160, y + 278, MUTED, false);

        windows.renderAll(context, client);

        super.render(context, mouseX, mouseY, delta);
    }

    private void shiftChannel(int delta) {
        if (channelIds.isEmpty()) {
            return;
        }

        selectedChannel = Math.floorMod(selectedChannel + delta, channelIds.size());
        reinitScreen();
    }

    private void renameCurrentChannel() {
        String name = channelNameField.getText().trim();
        if (!name.isEmpty()) {
            currentChannel().setName(name);
        }
    }

    private void toggleCustomColor() {
        ChatChannel channel = currentChannel();
        if (channel.hasCustomColor()) {
            channel.clearCustomColor();
        } else {
            int color = parseColor(colorField.getText(), channel.color());
            channel.setColor(color);
        }
        customColorButton.setMessage(customColorText());
    }

    private void toggleFilter(String id, ButtonWidget button) {
        String channelId = currentChannel().id();
        boolean current = filters.isFilterEnabled(channelId, id);
        filters.setFilterEnabled(channelId, id, !current);
        button.setMessage(filterButtonText(id));
    }

    private void toggleMinimized(ButtonWidget button) {
        ChatChannel channel = currentChannel();
        boolean minimized = channel.windowState() == WindowState.MINIMIZED;
        channel.setWindowState(minimized ? WindowState.OPEN : WindowState.MINIMIZED);
        ChannelWindow window = windows.getOrCreateDefault(channel.id());
        window.setMinimized(!minimized);
        button.setMessage(minimizedText());
    }

    private void createChannel() {
        String id = channelCreateField.getText().trim().toLowerCase();
        if (id.isEmpty()) {
            return;
        }

        RevageChatClient.instance().channels().getChannel(id).orElseGet(() ->
            RevageChatClient.instance().channels().createChannel(
                new ChatChannel(id, id, config.globalColorRgb(), 0.90F, 1.0F, WindowState.OPEN)
            )
        );
        reloadChannels();
        selectedChannel = Math.max(0, channelIds.indexOf(id));
        reinitScreen();
    }

    private void deleteChannel() {
        String id = currentChannel().id();
        if (RevageChatClient.instance().channels().removeChannel(id)) {
            windows.remove(id);
            reloadChannels();
            selectedChannel = Math.max(0, Math.min(selectedChannel, channelIds.size() - 1));
            reinitScreen();
        }
    }

    private void save() {
        ChatChannel channel = currentChannel();
        channel.setColor(parseColor(colorField.getText(), channel.color()));

        var channels = RevageChatClient.instance().channels().getChannels();
        config.saveChannels(channels);

        List<String> channelIds = new ArrayList<>();
        for (ChatChannel chatChannel : channels) {
            channelIds.add(chatChannel.id());
        }

        for (var channelEntry : filters.snapshotStates(channelIds).entrySet()) {
            for (var filterEntry : channelEntry.getValue().entrySet()) {
                config.setChannelFilterState(channelEntry.getKey(), filterEntry.getKey(), filterEntry.getValue());
            }
        }

        config.saveWindowLayouts(windows.snapshotLayouts());
        config.save();
        onSave.run();
    }

    private void reloadChannels() {
        channelIds.clear();
        for (ChatChannel channel : RevageChatClient.instance().channels().getChannels()) {
            channelIds.add(channel.id());
        }
        if (channelIds.isEmpty()) {
            channelIds.add("default");
        }
        selectedChannel = Math.max(0, Math.min(selectedChannel, channelIds.size() - 1));
    }

    private ChatChannel currentChannel() {
        String id = channelIds.get(selectedChannel);
        return RevageChatClient.instance().channels().getChannel(id)
            .orElseGet(() -> RevageChatClient.instance().channels().createChannel(
                new ChatChannel(id, id, 0xFFFFFF, 1.0F, 1.0F, WindowState.OPEN)
            ));
    }

    private String currentChannelHex() {
        return String.format("#%06X", currentChannel().color());
    }

    private Text customColorText() {
        return Text.literal("Custom color: " + (currentChannel().hasCustomColor() ? "ON" : "OFF"));
    }

    private Text minimizedText() {
        return Text.literal("Window: " + (currentChannel().windowState() == WindowState.MINIMIZED ? "MIN" : "OPEN"));
    }

    private Text filterButtonText(String id) {
        boolean enabled = filters.isFilterEnabled(currentChannel().id(), id);
        return Text.literal(id + ": " + (enabled ? "ON" : "OFF"));
    }

    private static int parseColor(String value, int fallback) {
        String v = value == null ? "" : value.trim();
        if (v.startsWith("#") && v.length() == 7) {
            try {
                return Integer.parseInt(v.substring(1), 16);
            } catch (NumberFormatException ignored) {
                return fallback;
            }
        }
        return fallback;
    }

    private void reinitScreen() {
        clearChildren();
        init();
    }

    @FunctionalInterface
    private interface SliderValueConsumer {
        void accept(double value);
    }

    private static final class PercentSlider extends SliderWidget {
        private final SliderValueConsumer consumer;
        private final String label;

        private PercentSlider(int x, int y, int width, int height, double value, SliderValueConsumer consumer, String label) {
            super(x, y, width, height, Text.empty(), value);
            this.consumer = consumer;
            this.label = label;
            updateMessage();
        }

        @Override
        protected void updateMessage() {
            setMessage(Text.literal(label + ": " + (int) Math.round(value * 100) + "%"));
        }

        @Override
        protected void applyValue() {
            consumer.accept(value);
        }
    }
}
