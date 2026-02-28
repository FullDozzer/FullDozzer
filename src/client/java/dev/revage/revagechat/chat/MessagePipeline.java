package dev.revage.revagechat.chat;

import dev.revage.revagechat.RevageChatClient;
import dev.revage.revagechat.audio.SoundManager;
import dev.revage.revagechat.chat.model.MessageContext;
import dev.revage.revagechat.filter.FilterEngine;
import dev.revage.revagechat.log.LogManager;
import dev.revage.revagechat.stats.StatisticsManager;

/**
 * Coordinates message processing steps for incoming and outgoing chat.
 */
public final class MessagePipeline {
    private final ChannelManager channelManager;
    private final FilterEngine filterEngine;
    private final LogManager logManager;
    private final SoundManager soundManager;
    private final StatisticsManager statisticsManager;

    public MessagePipeline(
        ChannelManager channelManager,
        FilterEngine filterEngine,
        LogManager logManager,
        SoundManager soundManager,
        StatisticsManager statisticsManager
    ) {
        this.channelManager = channelManager;
        this.filterEngine = filterEngine;
        this.logManager = logManager;
        this.soundManager = soundManager;
        this.statisticsManager = statisticsManager;
    }

    public boolean handleIncoming(MessageContext context) {
        String channel = channelManager.resolveChannel(context.originalText());

        if (!filterEngine.allow(context)) {
            RevageChatClient.LOGGER.debug("Incoming message blocked by filter on channel {}", channel);
            return false;
        }

        logManager.appendIncoming(context, channel);
        soundManager.playIncomingCue(channel);
        statisticsManager.markIncomingProcessed();

        // TODO: Add pipeline steps (transformations, highlights, command hooks).
        return true;
    }

    public boolean handleOutgoing(MessageContext context) {
        String channel = channelManager.resolveChannel(context.originalText());

        if (!filterEngine.allow(context)) {
            RevageChatClient.LOGGER.debug("Outgoing message blocked by filter on channel {}", channel);
            return false;
        }

        logManager.appendOutgoing(context, channel);
        statisticsManager.markOutgoingProcessed();

        // TODO: Add rate-limit and channel formatting stages.
        return true;
    }
}
