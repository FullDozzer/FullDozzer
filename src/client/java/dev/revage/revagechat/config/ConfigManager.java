package dev.revage.revagechat.config;

import dev.revage.revagechat.chat.ChatChannel;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;

/**
 * Owns loading/saving mod configuration data.
 */
public final class ConfigManager {

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
}
