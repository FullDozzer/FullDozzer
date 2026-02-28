package dev.revage.revagechat.ui;

import dev.revage.revagechat.config.ConfigManager;
import net.minecraft.client.gui.DrawContext;
import net.minecraft.client.gui.screen.Screen;
import net.minecraft.client.gui.tooltip.Tooltip;
import net.minecraft.client.gui.widget.ButtonWidget;
import net.minecraft.client.gui.widget.SliderWidget;
import net.minecraft.client.gui.widget.TextFieldWidget;
import net.minecraft.text.Text;

/**
 * Discord-style configuration screen for RevageChat.
 */
public final class RevageChatConfigScreen extends Screen {
    private static final int BG = 0xFF2B2D31;
    private static final int PANEL = 0xFF1E1F22;
    private static final int BORDER = 0xFF3A3C43;
    private static final int ACCENT = 0xFF5865F2;
    private static final int MUTED = 0xFFB5BAC1;

    private final Screen parent;
    private final ConfigManager config;
    private final Runnable onSave;

    private TextFieldWidget colorHexField;
    private ButtonWidget overrideButton;
    private ButtonWidget filtersButton;
    private VolumeSlider volumeSlider;

    public RevageChatConfigScreen(Screen parent, ConfigManager config, Runnable onSave) {
        super(Text.literal("RevageChat Settings"));
        this.parent = parent;
        this.config = config;
        this.onSave = onSave;
    }

    @Override
    protected void init() {
        int panelX = this.width / 2 - 170;
        int panelY = this.height / 2 - 110;

        this.colorHexField = new TextFieldWidget(textRenderer, panelX + 20, panelY + 46, 110, 20, Text.literal("Global color"));
        this.colorHexField.setText(String.format("#%06X", config.globalColorRgb()));
        this.colorHexField.setTooltip(Tooltip.of(Text.literal("Global message color (#RRGGBB)")));
        addDrawableChild(colorHexField);

        this.overrideButton = addDrawableChild(ButtonWidget.builder(buttonText(), button -> {
            config.setOverrideExistingColors(!config.overrideExistingColors());
            button.setMessage(buttonText());
        }).dimensions(panelX + 140, panelY + 46, 180, 20).build());

        this.filtersButton = addDrawableChild(ButtonWidget.builder(filtersText(), button -> {
            config.setFiltersEnabled(!config.filtersEnabled());
            button.setMessage(filtersText());
        }).dimensions(panelX + 20, panelY + 82, 300, 20).build());

        this.volumeSlider = addDrawableChild(new VolumeSlider(
            panelX + 20,
            panelY + 108,
            300,
            20,
            config.masterSoundVolume(),
            value -> config.setMasterSoundVolume((float) value)
        ));

        addDrawableChild(ButtonWidget.builder(Text.literal("Save"), button -> saveAndClose())
            .dimensions(panelX + 20, panelY + 150, 145, 22)
            .build());

        addDrawableChild(ButtonWidget.builder(Text.literal("Cancel"), button -> close())
            .dimensions(panelX + 175, panelY + 150, 145, 22)
            .build());
    }

    @Override
    public void close() {
        if (client != null) {
            client.setScreen(parent);
        }
    }

    @Override
    public void render(DrawContext context, int mouseX, int mouseY, float delta) {
        renderBackground(context, mouseX, mouseY, delta);

        int panelX = this.width / 2 - 170;
        int panelY = this.height / 2 - 110;
        int panelW = 340;
        int panelH = 220;

        context.fill(0, 0, width, height, BG);
        context.fill(panelX, panelY, panelX + panelW, panelY + panelH, PANEL);
        context.drawBorder(panelX, panelY, panelW, panelH, BORDER);
        context.fill(panelX, panelY, panelX + panelW, panelY + 28, ACCENT);

        context.drawText(textRenderer, Text.literal("RevageChat Settings"), panelX + 12, panelY + 9, 0xFFFFFFFF, false);
        context.drawText(textRenderer, Text.literal("Global Color"), panelX + 20, panelY + 34, MUTED, false);
        context.drawText(textRenderer, Text.literal("Filters"), panelX + 20, panelY + 70, MUTED, false);
        context.drawText(textRenderer, Text.literal("Master UI Volume"), panelX + 20, panelY + 96, MUTED, false);

        super.render(context, mouseX, mouseY, delta);
    }

    private void saveAndClose() {
        String value = colorHexField.getText().trim();
        if (value.startsWith("#") && value.length() == 7) {
            try {
                config.setGlobalColorRgb(Integer.parseInt(value.substring(1), 16));
            } catch (NumberFormatException ignored) {
                // keep old color
            }
        }

        config.save();
        onSave.run();
        close();
    }

    private Text buttonText() {
        return Text.literal("Override existing colors: " + (config.overrideExistingColors() ? "ON" : "OFF"));
    }

    private Text filtersText() {
        return Text.literal("Filters: " + (config.filtersEnabled() ? "ON" : "OFF"));
    }

    @FunctionalInterface
    private interface SliderValueConsumer {
        void accept(double value);
    }

    private static final class VolumeSlider extends SliderWidget {
        private final SliderValueConsumer consumer;

        private VolumeSlider(int x, int y, int width, int height, double value, SliderValueConsumer consumer) {
            super(x, y, width, height, Text.empty(), value);
            this.consumer = consumer;
            updateMessage();
        }

        @Override
        protected void updateMessage() {
            setMessage(Text.literal("Volume: " + (int) Math.round(value * 100) + "%"));
        }

        @Override
        protected void applyValue() {
            consumer.accept(value);
        }
    }
}
