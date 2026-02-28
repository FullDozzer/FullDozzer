package dev.revage.revagechat.chat;

import dev.revage.revagechat.RevageChatClient;
import dev.revage.revagechat.audio.SoundManager;
import dev.revage.revagechat.chat.model.MessageContext;
import dev.revage.revagechat.filter.FilterEngine;
import dev.revage.revagechat.log.LogManager;
import dev.revage.revagechat.stats.StatisticsManager;
import java.util.ArrayList;
import java.util.List;

/**
 * Coordinates message processing steps for incoming and outgoing chat.
 */
public final class MessagePipeline {
    private final ChannelManager channelManager;
    private final FilterEngine filterEngine;
    private final LogManager logManager;
    private final SoundManager soundManager;
    private final StatisticsManager statisticsManager;

    private final List<ChatFilter> preFilters;
    private final List<ChatTransformer> transformers;
    private final List<ChatPostAction> postActions;

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

        this.preFilters = new ArrayList<>(4);
        this.transformers = new ArrayList<>(4);
        this.postActions = new ArrayList<>(4);
    }

    public List<ChatFilter> preFilters() {
        return preFilters;
    }

    public List<ChatTransformer> transformers() {
        return transformers;
    }

    public List<ChatPostAction> postActions() {
        return postActions;
    }

    public boolean handleIncoming(MessageContext context) {
        RoutingResult routing = channelManager.resolveRouting(context);
        List<ChatChannel> targets = routing.targetChannels();

        if (!passesPreFilters(context, targets)) {
            return false;
        }

        for (int i = 0, size = targets.size(); i < size; i++) {
            ChatChannel channel = targets.get(i);

            if (!passesChannelFilters(context, channel)) {
                continue;
            }

            String formattedText = applyTransformers(context, channel);

            MessageContext routedContext = context;
            if (!formattedText.equals(context.formattedText())) {
                routedContext = new MessageContext(
                    context.originalText(),
                    formattedText,
                    context.senderName(),
                    context.uuid(),
                    context.timestamp(),
                    context.messageType()
                );
            }

            channel.appendHistory(routedContext);
            logManager.appendIncoming(routedContext, channel.id());
            soundManager.playIncomingCue(channel.id());
        }

        runPostActions(context, targets, routing.hideFromDefaultChat());
        statisticsManager.markIncomingProcessed();

        return !routing.hideFromDefaultChat();
    }

    public boolean handleOutgoing(MessageContext context) {
        RoutingResult routing = channelManager.resolveRouting(context);

        if (!passesPreFilters(context, routing.targetChannels())) {
            return false;
        }

        if (!filterEngine.allow(context)) {
            RevageChatClient.LOGGER.debug("Outgoing message blocked by filter engine");
            return false;
        }

        List<ChatChannel> targets = routing.targetChannels();
        for (int i = 0, size = targets.size(); i < size; i++) {
            ChatChannel channel = targets.get(i);
            logManager.appendOutgoing(context, channel.id());
        }

        runPostActions(context, targets, routing.hideFromDefaultChat());
        statisticsManager.markOutgoingProcessed();

        return true;
    }

    private boolean passesPreFilters(MessageContext context, List<ChatChannel> targets) {
        if (!filterEngine.allow(context)) {
            RevageChatClient.LOGGER.debug("Message blocked by filter engine");
            return false;
        }

        ChatChannel primary = targets.isEmpty() ? null : targets.get(0);
        for (int i = 0, size = preFilters.size(); i < size; i++) {
            if (!preFilters.get(i).allow(context, primary)) {
                return false;
            }
        }

        return true;
    }

    private boolean passesChannelFilters(MessageContext context, ChatChannel channel) {
        List<ChatFilter> channelFilters = channel.filters();
        for (int i = 0, size = channelFilters.size(); i < size; i++) {
            if (!channelFilters.get(i).allow(context, channel)) {
                return false;
            }
        }

        return true;
    }

    private String applyTransformers(MessageContext context, ChatChannel channel) {
        String formattedText = context.formattedText();

        for (int i = 0, size = transformers.size(); i < size; i++) {
            formattedText = transformers.get(i).transform(context, channel, formattedText);
        }

        return formattedText;
    }

    private void runPostActions(MessageContext context, List<ChatChannel> targets, boolean hideFromDefault) {
        for (int i = 0, size = postActions.size(); i < size; i++) {
            postActions.get(i).run(context, targets, hideFromDefault);
        }
    }
}
