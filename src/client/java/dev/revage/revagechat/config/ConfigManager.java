package dev.revage.revagechat.config;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import dev.revage.revagechat.RevageChatClient;
import dev.revage.revagechat.chat.ChatChannel;
import dev.revage.revagechat.chat.WindowState;
import java.io.IOException;
import java.io.Reader;
import java.io.Writer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
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

    public ConfigManager() {
        this.overrideExistingColors = false;
        this.globalColorRgb = 0xFFFFFF;
        this.masterSoundVolume = 1.0F;
        this.filtersEnabled = true;
        this.channels = new ArrayList<>();
    }

    public void load() {
        try {
            Files.createDirectories(CONFIG_DIR);
        } catch (IOException exception) {
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
}
