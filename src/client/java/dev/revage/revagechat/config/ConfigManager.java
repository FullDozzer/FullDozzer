package dev.revage.revagechat.config;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import dev.revage.revagechat.RevageChatClient;
import dev.revage.revagechat.chat.ChatChannel;
import dev.revage.revagechat.chat.WindowState;
import dev.revage.revagechat.chat.window.ChannelWindowManager;
import java.io.Reader;
import java.io.Writer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import net.fabricmc.loader.api.FabricLoader;

/**
 * Owns loading/saving mod configuration data.
 */
public final class ConfigManager {
    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().create();
    private static final Path CONFIG_DIR = FabricLoader.getInstance().getConfigDir().resolve("revagechat");
    private static final Path CONFIG_FILE = CONFIG_DIR.resolve("config.json");

    private boolean overrideExistingColors;
    private int globalColorRgb;
    private float masterSoundVolume;
    private boolean filtersEnabled;
    private final List<ChatChannel> channels;
    private final Map<String, Map<String, Boolean>> channelFilterStates;
    private final Map<String, ChannelWindowManager.WindowLayout> windowLayouts;

    public ConfigManager() {
        this.overrideExistingColors = false;
        this.globalColorRgb = 0xFFFFFF;
        this.masterSoundVolume = 1.0F;
        this.filtersEnabled = true;
        this.channels = new ArrayList<>();
        this.channelFilterStates = new LinkedHashMap<>();
        this.windowLayouts = new LinkedHashMap<>();
    }

    public void load() {
        try {
            Files.createDirectories(CONFIG_DIR);
        } catch (Exception exception) {
            RevageChatClient.LOGGER.warn("Could not create config directory", exception);
            return;
        }

        if (!Files.exists(CONFIG_FILE)) {
            save();
            return;
        }

        try (Reader reader = Files.newBufferedReader(CONFIG_FILE)) {
            ConfigData data = GSON.fromJson(reader, ConfigData.class);
            if (data == null) {
                return;
            }

            this.overrideExistingColors = data.overrideExistingColors;
            this.globalColorRgb = data.globalColorRgb;
            this.masterSoundVolume = clamp01(data.masterSoundVolume);
            this.filtersEnabled = data.filtersEnabled;
            this.channels.clear();
            this.channelFilterStates.clear();
            this.windowLayouts.clear();

            if (data.channels != null) {
                for (ChannelData channelData : data.channels) {
                    ChatChannel channel = new ChatChannel(
                        channelData.id,
                        channelData.name,
                        channelData.color,
                        channelData.opacity,
                        channelData.volume,
                        parseWindowState(channelData.windowState)
                    );
                    if (channelData.customColor) {
                        channel.setColor(channelData.color);
                    }
                    this.channels.add(channel);
                }
            }

            if (data.channelFilterStates != null) {
                for (Map.Entry<String, Map<String, Boolean>> entry : data.channelFilterStates.entrySet()) {
                    channelFilterStates.put(entry.getKey(), new LinkedHashMap<>(entry.getValue()));
                }
            }

            if (data.windowLayouts != null) {
                for (Map.Entry<String, WindowLayoutData> entry : data.windowLayouts.entrySet()) {
                    WindowLayoutData layout = entry.getValue();
                    if (layout == null) {
                        continue;
                    }
                    windowLayouts.put(entry.getKey(), new ChannelWindowManager.WindowLayout(
                        layout.x,
                        layout.y,
                        layout.width,
                        layout.height,
                        clamp01(layout.opacity),
                        layout.minimized
                    ));
                }
            }
        } catch (Exception exception) {
            RevageChatClient.LOGGER.warn("Could not read config file", exception);
        }
    }

    public void save() {
        try {
            Files.createDirectories(CONFIG_DIR);
            ConfigData data = new ConfigData();
            data.overrideExistingColors = overrideExistingColors;
            data.globalColorRgb = globalColorRgb;
            data.masterSoundVolume = masterSoundVolume;
            data.filtersEnabled = filtersEnabled;
            data.channels = new ArrayList<>(channels.size());
            data.channelFilterStates = new LinkedHashMap<>(channelFilterStates);
            data.windowLayouts = new LinkedHashMap<>();

            for (ChatChannel channel : channels) {
                ChannelData channelData = new ChannelData();
                channelData.id = channel.id();
                channelData.name = channel.name();
                channelData.color = channel.color();
                channelData.customColor = channel.hasCustomColor();
                channelData.opacity = channel.opacity();
                channelData.volume = channel.volume();
                channelData.windowState = channel.windowState().name();
                data.channels.add(channelData);
            }

            for (Map.Entry<String, ChannelWindowManager.WindowLayout> entry : windowLayouts.entrySet()) {
                ChannelWindowManager.WindowLayout layout = entry.getValue();
                WindowLayoutData layoutData = new WindowLayoutData();
                layoutData.x = layout.x();
                layoutData.y = layout.y();
                layoutData.width = layout.width();
                layoutData.height = layout.height();
                layoutData.opacity = layout.opacity();
                layoutData.minimized = layout.minimized();
                data.windowLayouts.put(entry.getKey(), layoutData);
            }

            try (Writer writer = Files.newBufferedWriter(CONFIG_FILE)) {
                GSON.toJson(data, writer);
            }
        } catch (Exception exception) {
            RevageChatClient.LOGGER.warn("Could not write config file", exception);
        }
    }

    public List<ChatChannel> loadChannels() {
        return new ArrayList<>(channels);
    }

    public void saveChannels(Collection<ChatChannel> channels) {
        this.channels.clear();
        this.channels.addAll(channels);
        save();
    }

    public Map<String, Map<String, Boolean>> channelFilterStates() {
        Map<String, Map<String, Boolean>> copy = new LinkedHashMap<>();
        for (Map.Entry<String, Map<String, Boolean>> entry : channelFilterStates.entrySet()) {
            copy.put(entry.getKey(), new LinkedHashMap<>(entry.getValue()));
        }
        return copy;
    }

    public void setChannelFilterState(String channelId, String filterId, boolean enabled) {
        channelFilterStates.computeIfAbsent(channelId, ignored -> new LinkedHashMap<>()).put(filterId, enabled);
    }

    public Map<String, ChannelWindowManager.WindowLayout> windowLayouts() {
        return new LinkedHashMap<>(windowLayouts);
    }

    public void saveWindowLayouts(Map<String, ChannelWindowManager.WindowLayout> layouts) {
        this.windowLayouts.clear();
        this.windowLayouts.putAll(layouts);
    }

    public boolean overrideExistingColors() {
        return overrideExistingColors;
    }

    public void setOverrideExistingColors(boolean overrideExistingColors) {
        this.overrideExistingColors = overrideExistingColors;
    }

    public int globalColorRgb() {
        return globalColorRgb;
    }

    public void setGlobalColorRgb(int globalColorRgb) {
        this.globalColorRgb = globalColorRgb & 0xFFFFFF;
    }

    public float masterSoundVolume() {
        return masterSoundVolume;
    }

    public void setMasterSoundVolume(float masterSoundVolume) {
        this.masterSoundVolume = clamp01(masterSoundVolume);
    }

    public boolean filtersEnabled() {
        return filtersEnabled;
    }

    public void setFiltersEnabled(boolean filtersEnabled) {
        this.filtersEnabled = filtersEnabled;
    }

    private static WindowState parseWindowState(String value) {
        try {
            return WindowState.valueOf(value);
        } catch (Exception ignored) {
            return WindowState.OPEN;
        }
    }

    private static float clamp01(float value) {
        return Math.max(0.0F, Math.min(1.0F, value));
    }

    private static final class ConfigData {
        private boolean overrideExistingColors;
        private int globalColorRgb;
        private float masterSoundVolume = 1.0F;
        private boolean filtersEnabled = true;
        private List<ChannelData> channels;
        private Map<String, Map<String, Boolean>> channelFilterStates;
        private Map<String, WindowLayoutData> windowLayouts;
    }

    private static final class ChannelData {
        private String id;
        private String name;
        private int color;
        private boolean customColor;
        private float opacity;
        private float volume;
        private String windowState;
    }

    private static final class WindowLayoutData {
        private int x;
        private int y;
        private int width;
        private int height;
        private float opacity = 1.0F;
        private boolean minimized;
    }
}
