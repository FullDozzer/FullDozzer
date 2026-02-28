package dev.revage.revagechat.audio;

import dev.revage.revagechat.RevageChatClient;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.sound.PositionedSoundInstance;
import net.minecraft.sound.SoundEvent;
import net.minecraft.sound.SoundEvents;
import net.minecraft.util.Identifier;

/**
 * Handles chat-related sound cues.
 */
public final class SoundManager {
    private static final Path SOUNDS_DIR = Path.of("config", "revagechat", "sounds");
    private static final float DEFAULT_CHANNEL_VOLUME = 1.0F;

    private final Map<String, Float> channelVolumes;
    private final Map<String, Path> customSoundFiles;
    private final Set<String> mutedFilterActions;

    public SoundManager() {
        this.channelVolumes = new ConcurrentHashMap<>();
        this.customSoundFiles = new ConcurrentHashMap<>();
        this.mutedFilterActions = ConcurrentHashMap.newKeySet();

        reloadCustomSounds();
    }

    public void reloadCustomSounds() {
        customSoundFiles.clear();

        try {
            Files.createDirectories(SOUNDS_DIR);
        } catch (IOException exception) {
            RevageChatClient.LOGGER.warn("Could not create sounds directory: {}", SOUNDS_DIR, exception);
            return;
        }

        try (var stream = Files.list(SOUNDS_DIR)) {
            stream.filter(path -> Files.isRegularFile(path) && path.getFileName().toString().toLowerCase().endsWith(".ogg"))
                .forEach(path -> {
                    String fileName = path.getFileName().toString();
                    int dot = fileName.lastIndexOf('.');
                    String key = dot > 0 ? fileName.substring(0, dot).toLowerCase() : fileName.toLowerCase();
                    customSoundFiles.put(key, path);
                });
        } catch (IOException exception) {
            RevageChatClient.LOGGER.warn("Could not scan sound directory: {}", SOUNDS_DIR, exception);
        }
    }

    public void setChannelVolume(String channelId, float volume) {
        channelVolumes.put(channelId, clamp(volume));
    }

    public float getChannelVolume(String channelId) {
        return channelVolumes.getOrDefault(channelId, DEFAULT_CHANNEL_VOLUME);
    }

    public void setFilterActionMuted(String actionKey, boolean muted) {
        String key = normalizeAction(actionKey);
        if (muted) {
            mutedFilterActions.add(key);
        } else {
            mutedFilterActions.remove(key);
        }
    }

    public boolean isFilterActionMuted(String actionKey) {
        return mutedFilterActions.contains(normalizeAction(actionKey));
    }

    public void playIncomingCue(String channelId) {
        playIncomingCue(channelId, null);
    }

    /**
     * Plays UI chat cue for a channel unless muted by filter action.
     */
    public void playIncomingCue(String channelId, String filterActionKey) {
        if (filterActionKey != null && isFilterActionMuted(filterActionKey)) {
            return;
        }

        MinecraftClient client = MinecraftClient.getInstance();
        if (client == null || client.getSoundManager() == null) {
            return;
        }

        float volume = getChannelVolume(channelId);
        String key = channelId == null ? "default" : channelId.toLowerCase();

        Path soundFile = customSoundFiles.get(key);
        if (soundFile == null) {
            soundFile = customSoundFiles.get("default");
        }

        // Safe fallback: play built-in UI click when custom sound cannot be used as a resource-backed event.
        if (soundFile == null || !Files.exists(soundFile)) {
            client.getSoundManager().play(PositionedSoundInstance.master(SoundEvents.UI_BUTTON_CLICK.value(), volume));
            return;
        }

        try {
            Identifier id = Identifier.of(RevageChatClient.MOD_ID, "external/" + key);
            SoundEvent externalEvent = SoundEvent.of(id);
            client.getSoundManager().play(PositionedSoundInstance.master(externalEvent, volume));
        } catch (Exception exception) {
            RevageChatClient.LOGGER.debug("Custom sound failed for channel '{}', using fallback: {}", key, soundFile, exception);
            client.getSoundManager().play(PositionedSoundInstance.master(SoundEvents.UI_BUTTON_CLICK.value(), volume));
        }
    }

    private static String normalizeAction(String value) {
        return value == null ? "" : value.trim().toLowerCase();
    }

    private static float clamp(float value) {
        return Math.max(0.0F, Math.min(1.0F, value));
    }
}
