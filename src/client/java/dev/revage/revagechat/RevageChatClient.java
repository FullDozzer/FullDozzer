package dev.revage.revagechat;

import com.mojang.brigadier.arguments.BoolArgumentType;
import dev.revage.revagechat.audio.SoundManager;
import dev.revage.revagechat.chat.ChannelManager;
import dev.revage.revagechat.chat.ChatInterceptor;
import dev.revage.revagechat.chat.MessagePipeline;
import dev.revage.revagechat.chat.MessageType;
import dev.revage.revagechat.chat.model.MessageContext;
import dev.revage.revagechat.chat.window.ChannelWindowManager;
import dev.revage.revagechat.config.ConfigManager;
import dev.revage.revagechat.filter.FilterEngine;
import dev.revage.revagechat.log.LogManager;
import dev.revage.revagechat.stats.StatisticsManager;
import dev.revage.revagechat.ui.RevageChatConfigScreen;
import dev.revage.revagechat.ui.RevageChatStudioScreen;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.UUID;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.command.v2.ClientCommandRegistrationCallback;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.keybinding.v1.KeyBindingHelper;
import net.fabricmc.fabric.api.client.message.v1.ClientSendMessageEvents;
import net.fabricmc.fabric.api.client.rendering.v1.HudRenderCallback;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.option.KeyBinding;
import net.minecraft.client.util.InputUtil;
import net.minecraft.text.Text;
import org.jetbrains.annotations.Nullable;
import org.lwjgl.glfw.GLFW;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import static net.fabricmc.fabric.api.client.command.v2.ClientCommandManager.argument;
import static net.fabricmc.fabric.api.client.command.v2.ClientCommandManager.literal;

/**
 * Client entrypoint for RevageChat.
 */
public final class RevageChatClient implements ClientModInitializer {
    public static final String MOD_ID = "revagechat";
    public static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    private static RevageChatClient instance;

    private ConfigManager configManager;
    private ChannelManager channelManager;
    private FilterEngine filterEngine;
    private MessagePipeline messagePipeline;
    private SoundManager soundManager;
    private StatisticsManager statisticsManager;
    private LogManager logManager;
    private ChatInterceptor chatInterceptor;
    private ChannelWindowManager windowManager;
    private KeyBinding openSettingsKey;
    private KeyBinding openWindowKey;
    private KeyBinding openStudioKey;

    @Override
    public void onInitializeClient() {
        instance = this;

        LOGGER.info("Initializing {}", MOD_ID);

        initializeManagers();
        registerEvents();
        registerCommands();

        LOGGER.info("{} initialized", MOD_ID);
    }

    public static RevageChatClient instance() {
        return instance;
    }

    public ConfigManager config() {
        return configManager;
    }

    public SoundManager sound() {
        return soundManager;
    }

    public ChannelManager channels() {
        return channelManager;
    }

    public ChannelWindowManager windows() {
        return windowManager;
    }

    public FilterEngine filterEngine() {
        return filterEngine;
    }


    public boolean onWindowMouseDown(double mouseX, double mouseY, int button) {
        return windowManager.onMouseDown(mouseX, mouseY, button);
    }

    public boolean onWindowMouseDrag(double mouseX, double mouseY, int button) {
        return windowManager.onMouseDrag(mouseX, mouseY, button);
    }

    public boolean onWindowMouseUp(int button) {
        return windowManager.onMouseUp(button);
    }

    public boolean onWindowMouseScroll(double mouseX, double mouseY, double amount) {
        return windowManager.onMouseScroll(mouseX, mouseY, amount);
    }


    public static boolean interceptIncomingFromMixin(
        String originalText,
        String formattedText,
        String senderName,
        @Nullable UUID uuid,
        MessageType messageType
    ) {
        if (instance == null || instance.chatInterceptor == null) {
            return true;
        }

        MessageContext context = new MessageContext(
            originalText,
            formattedText,
            senderName,
            uuid,
            Instant.now(),
            messageType
        );

        return instance.chatInterceptor.onIncomingMessage(context);
    }

    private void initializeManagers() {
        this.configManager = new ConfigManager();
        this.configManager.load();

        this.channelManager = new ChannelManager(configManager);
        this.channelManager.load();
        this.filterEngine = new FilterEngine();
        this.filterEngine.setEnabled(configManager.filtersEnabled());
        this.filterEngine.applyPersistedStates(configManager.channelFilterStates());
        this.soundManager = new SoundManager();
        this.soundManager.setMasterVolume(configManager.masterSoundVolume());
        this.statisticsManager = new StatisticsManager();
        this.logManager = new LogManager();
        this.windowManager = new ChannelWindowManager();
        this.windowManager.getOrCreateDefault("default");
        this.windowManager.applyLayouts(configManager.windowLayouts());

        this.messagePipeline = new MessagePipeline(
            channelManager,
            configManager,
            filterEngine,
            logManager,
            soundManager,
            statisticsManager,
            windowManager
        );
        this.chatInterceptor = new ChatInterceptor(messagePipeline, statisticsManager);
    }

    private void registerEvents() {
        this.openSettingsKey = KeyBindingHelper.registerKeyBinding(new KeyBinding(
            "key.revagechat.open_settings",
            InputUtil.Type.KEYSYM,
            GLFW.GLFW_KEY_RIGHT_SHIFT,
            "category.revagechat"
        ));

        this.openWindowKey = KeyBindingHelper.registerKeyBinding(new KeyBinding(
            "key.revagechat.open_default_window",
            InputUtil.Type.KEYSYM,
            GLFW.GLFW_KEY_GRAVE_ACCENT,
            "category.revagechat"
        ));

        this.openStudioKey = KeyBindingHelper.registerKeyBinding(new KeyBinding(
            "key.revagechat.open_studio",
            InputUtil.Type.KEYSYM,
            GLFW.GLFW_KEY_RIGHT_CONTROL,
            "category.revagechat"
        ));

        ClientTickEvents.END_CLIENT_TICK.register(client -> {
            chatInterceptor.onClientTick(client);
            onClientTick(client);
            windowManager.tick(client, 1.0F);
        });

        HudRenderCallback.EVENT.register((drawContext, tickCounter) -> {
            MinecraftClient client = MinecraftClient.getInstance();
            if (client == null || client.options.hudHidden) {
                return;
            }
            windowManager.renderAll(drawContext, client);
        });

        ClientSendMessageEvents.ALLOW_CHAT.register(message -> {
            MessageContext context = new MessageContext(
                message,
                message,
                "self",
                null,
                Instant.now(),
                MessageType.OUTGOING
            );

            return chatInterceptor.onOutgoingMessage(context);
        });

        LOGGER.debug("Client events registered");
    }

    private void registerCommands() {
        ClientCommandRegistrationCallback.EVENT.register((dispatcher, registryAccess) -> dispatcher.register(
            literal("rc")
                .then(literal("menu").executes(ctx -> {
                    MinecraftClient client = MinecraftClient.getInstance();
                    client.setScreen(new RevageChatConfigScreen(client.currentScreen, configManager, this::applyConfigAtRuntime));
                    return 1;
                }))
                .then(literal("window").executes(ctx -> {
                    windowManager.getOrCreateDefault("default");
                    ctx.getSource().sendFeedback(Text.literal("RevageChat: default window opened"));
                    return 1;
                }))
                .then(literal("studio").executes(ctx -> {
                    MinecraftClient client = MinecraftClient.getInstance();
                    client.setScreen(new RevageChatStudioScreen(client.currentScreen, configManager, filterEngine, windowManager, this::applyConfigAtRuntime));
                    return 1;
                }))
                .then(literal("stats")
                    .then(literal("export").executes(ctx -> {
                        exportStats(ctx.getSource().getClient());
                        ctx.getSource().sendFeedback(Text.literal("RevageChat: stats exported"));
                        return 1;
                    })))
                .then(literal("filter")
                    .executes(ctx -> {
                        ctx.getSource().sendFeedback(Text.literal("RevageChat filters: " + (filterEngine.isEnabled() ? "ON" : "OFF")));
                        return 1;
                    })
                    .then(argument("enabled", BoolArgumentType.bool()).executes(ctx -> {
                        boolean enabled = BoolArgumentType.getBool(ctx, "enabled");
                        filterEngine.setEnabled(enabled);
                        configManager.setFiltersEnabled(enabled);
                        configManager.save();
                        ctx.getSource().sendFeedback(Text.literal("RevageChat filters: " + (enabled ? "ON" : "OFF")));
                        return 1;
                    })))
        ));
    }

    private void applyConfigAtRuntime() {
        soundManager.setMasterVolume(configManager.masterSoundVolume());
        filterEngine.setEnabled(configManager.filtersEnabled());

        var channels = channelManager.getChannels();
        var channelIds = new java.util.ArrayList<String>(channels.size());
        for (var channel : channels) {
            channelIds.add(channel.id());
        }

        for (var channelEntry : filterEngine.snapshotStates(channelIds).entrySet()) {
            for (var filterEntry : channelEntry.getValue().entrySet()) {
                configManager.setChannelFilterState(channelEntry.getKey(), filterEntry.getKey(), filterEntry.getValue());
            }
        }

        configManager.saveWindowLayouts(windowManager.snapshotLayouts());
        channelManager.save();
        configManager.save();
    }

    private void onClientTick(MinecraftClient client) {
        while (openSettingsKey.wasPressed()) {
            client.setScreen(new RevageChatConfigScreen(client.currentScreen, configManager, this::applyConfigAtRuntime));
        }

        while (openWindowKey.wasPressed()) {
            windowManager.getOrCreateDefault("default");
        }

        while (openStudioKey.wasPressed()) {
            client.setScreen(new RevageChatStudioScreen(client.currentScreen, configManager, filterEngine, windowManager, this::applyConfigAtRuntime));
        }
    }

    private void exportStats(MinecraftClient client) {
        Path dir = FabricLoader.getInstance().getConfigDir().resolve("revagechat");
        Path file = dir.resolve("stats-export.json");

        try {
            Files.createDirectories(dir);
            Files.writeString(file, statisticsManager.exportJson(25, 50));
        } catch (IOException exception) {
            LOGGER.warn("Could not export stats", exception);
            if (client.player != null) {
                client.player.sendMessage(Text.literal("RevageChat: failed to export stats"), false);
            }
        }
    }
}
