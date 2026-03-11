package dev.revage.revagechat.chat;

import dev.revage.revagechat.RevageChatClient;
import dev.revage.revagechat.chat.model.MessageContext;
import dev.revage.revagechat.stats.StatisticsManager;
import net.minecraft.client.MinecraftClient;

/**
 * Bridges Fabric and mixin hooks to the internal message pipeline.
 */
public final class ChatInterceptor {
    private final MessagePipeline messagePipeline;
    private final StatisticsManager statisticsManager;

    public ChatInterceptor(MessagePipeline messagePipeline, StatisticsManager statisticsManager) {
        this.messagePipeline = messagePipeline;
        this.statisticsManager = statisticsManager;
    }

    public void onClientTick(MinecraftClient client) {
        // reserved for deferred actions
    }

    public boolean onIncomingMessage(MessageContext context) {
        try {
            return messagePipeline.handleIncoming(context);
        } catch (Throwable throwable) {
            statisticsManager.recordHiddenMessage();
            RevageChatClient.LOGGER.error("Incoming pipeline failed, allowing vanilla render", throwable);
            return true;
        }
    }

    public boolean onOutgoingMessage(MessageContext context) {
        try {
            return messagePipeline.handleOutgoing(context);
        } catch (Throwable throwable) {
            RevageChatClient.LOGGER.error("Outgoing pipeline failed, allowing vanilla send", throwable);
            return true;
        }
    }
}
