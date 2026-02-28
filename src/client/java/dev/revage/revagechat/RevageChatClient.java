package dev.revage.revagechat;

import dev.revage.revagechat.audio.SoundManager;
import dev.revage.revagechat.chat.ChannelManager;
import dev.revage.revagechat.chat.ChatInterceptor;
import dev.revage.revagechat.chat.MessagePipeline;
import dev.revage.revagechat.chat.MessageType;
import dev.revage.revagechat.chat.model.MessageContext;
import dev.revage.revagechat.config.ConfigManager;
import dev.revage.revagechat.filter.FilterEngine;
import dev.revage.revagechat.log.LogManager;
import dev.revage.revagechat.stats.StatisticsManager;
import java.time.Instant;
import java.util.UUID;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.message.v1.ClientSendMessageEvents;
import org.jetbrains.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Client entrypoint for RevageChat.
 *
 * <p>This class wires high-level collaborators and registers Fabric client events.
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

    @Override
    public void onInitializeClient() {
        instance = this;

        LOGGER.info("Initializing {}", MOD_ID);

        initializeManagers();
        registerEvents();

        LOGGER.info("{} initialized", MOD_ID);
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
        // TODO: Load persisted config and user profile preferences.
        this.configManager = new ConfigManager();
        this.channelManager = new ChannelManager(configManager);
        this.channelManager.load();
        this.filterEngine = new FilterEngine();
        this.soundManager = new SoundManager();
        this.statisticsManager = new StatisticsManager();
        this.logManager = new LogManager();

        // TODO: Replace with dedicated dependency graph when concrete behaviors are added.
        this.messagePipeline = new MessagePipeline(
            channelManager,
            filterEngine,
            logManager,
            soundManager,
            statisticsManager
        );
        this.chatInterceptor = new ChatInterceptor(messagePipeline, statisticsManager);
    }

    private void registerEvents() {
        ClientTickEvents.END_CLIENT_TICK.register(chatInterceptor::onClientTick);

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
}
