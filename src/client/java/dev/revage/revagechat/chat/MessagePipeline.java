package dev.revage.revagechat.chat;

import dev.revage.revagechat.RevageChatClient;
import dev.revage.revagechat.audio.SoundManager;
import dev.revage.revagechat.chat.model.MessageContext;
import dev.revage.revagechat.chat.window.ChannelWindowManager;
import dev.revage.revagechat.config.ConfigManager;
import dev.revage.revagechat.filter.FilterActionStore;
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
    private final ConfigManager configManager;
    private final FilterEngine filterEngine;
    private final LogManager logManager;
    private final SoundManager soundManager;
    private final StatisticsManager statisticsManager;
    private final ChannelWindowManager windowManager;

    private final List<ChatFilter> preFilters;
    private final List<ChatTransformer> transformers;
    private final List<ChatPostAction> postActions;

    public MessagePipeline(
        ChannelManager channelManager,
        ConfigManager configManager,
        FilterEngine filterEngine,
        LogManager logManager,
        SoundManager soundManager,
        StatisticsManager statisticsManager,
        ChannelWindowManager windowManager
    ) {
        this.channelManager = channelManager;
        this.configManager = configManager;
        this.filterEngine = filterEngine;
        this.logManager = logManager;
        this.soundManager = soundManager;
        this.statisticsManager = statisticsManager;
        this.windowManager = windowManager;

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
            statisticsManager.recordHiddenMessage();
            return false;
        }

        boolean allowed = true;

        for (int i = 0, size = targets.size(); i < size; i++) {
            ChatChannel channel = targets.get(i);
            FilterActionStore.clear(context);

            if (!filterEngine.apply(channel.id(), context) || FilterActionStore.isBlocked(context)) {
                allowed = false;
                continue;
            }

            if (!passesChannelFilters(context, channel)) {
                continue;
            }

            String filterText = FilterActionStore.text(context);
            MessageContext withFilter = filterText.equals(context.formattedText()) ? context : new MessageContext(
                context.originalText(),
                filterText,
                context.senderName(),
                context.uuid(),
                context.timestamp(),
                context.messageType()
            );

            String colorizedText = applyColor(withFilter, channel);
            MessageContext withColor = colorizedText.equals(withFilter.formattedText()) ? withFilter : new MessageContext(
                withFilter.originalText(),
                colorizedText,
                withFilter.senderName(),
                withFilter.uuid(),
                withFilter.timestamp(),
                withFilter.messageType()
            );

            String transformedText = applyTransformers(withColor, channel);
            String taggedText = applyTag(transformedText, context);
            MessageContext routedContext = taggedText.equals(withColor.formattedText()) ? withColor : new MessageContext(
                withColor.originalText(),
                taggedText,
                withColor.senderName(),
                withColor.uuid(),
                withColor.timestamp(),
                withColor.messageType()
            );

            channel.appendHistory(routedContext);
            windowManager.appendMessage(channel.id(), routedContext.formattedText());
            logManager.appendIncoming(routedContext, channel.id());
            soundManager.playIncomingCue(channel.id(), FilterActionStore.tag(context));

            if (FilterActionStore.autoReply(context) != null) {
                RevageChatClient.LOGGER.info("AutoReply queued: {}", FilterActionStore.autoReply(context));
            }
            if (FilterActionStore.autoCommand(context) != null) {
                RevageChatClient.LOGGER.info("AutoCommand queued: {}", FilterActionStore.autoCommand(context));
            }
        }

        runPostActions(context, targets, routing.hideFromDefaultChat());
        statisticsManager.recordIncoming(context);
        statisticsManager.markIncomingProcessed();

        return allowed && !routing.hideFromDefaultChat();
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
            FilterActionStore.clear(context);
            if (!filterEngine.apply(channel.id(), context) || FilterActionStore.isBlocked(context)) {
                continue;
            }

            String source = FilterActionStore.text(context);
            MessageContext candidate = source.equals(context.formattedText()) ? context : new MessageContext(
                context.originalText(),
                source,
                context.senderName(),
                context.uuid(),
                context.timestamp(),
                context.messageType()
            );

            String colored = applyColor(candidate, channel);
            MessageContext channelContext = colored.equals(candidate.formattedText()) ? candidate : new MessageContext(
                candidate.originalText(),
                colored,
                candidate.senderName(),
                candidate.uuid(),
                candidate.timestamp(),
                candidate.messageType()
            );

            logManager.appendOutgoing(channelContext, channel.id());
        }

        runPostActions(context, targets, routing.hideFromDefaultChat());
        statisticsManager.recordOutgoing(context);
        statisticsManager.markOutgoingProcessed();

        return true;
    }

    private String applyColor(MessageContext context, ChatChannel channel) {
        Integer fallbackColor = channel.hasCustomColor() ? channel.color() : configManager.globalColorRgb();

        Integer recolor = FilterActionStore.recolor(context);
        if (recolor != null) {
            fallbackColor = recolor;
        }

        return ColorParser.toMinecraftFormatting(
            context.formattedText(),
            fallbackColor,
            configManager.overrideExistingColors()
        );
    }

    private String applyTag(String text, MessageContext context) {
        String tag = FilterActionStore.tag(context);
        if (tag == null || tag.isBlank()) {
            return text;
        }
        return "[" + tag + "] " + text;
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
