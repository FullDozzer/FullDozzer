package dev.revage.revagechat.config;

import dev.revage.revagechat.chat.ChatChannel;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;

/**
 * Owns loading/saving mod configuration data.
 */
public final class ConfigManager {
    private boolean overrideExistingColors;
    private int globalColorRgb;

    public ConfigManager() {
        this.overrideExistingColors = false;
        this.globalColorRgb = 0xFFFFFF;
    }

    public void load() {
        // TODO: Read JSON config from Fabric config directory.
    }

    public void save() {
        // TODO: Persist config changes to disk.
    }

    public List<ChatChannel> loadChannels() {
        // TODO: Deserialize persisted channel definitions.
        return new ArrayList<>(0);
    }

    public void saveChannels(Collection<ChatChannel> channels) {
        // TODO: Serialize channel definitions and flush to config file.
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
        this.globalColorRgb = globalColorRgb;
    }
}
