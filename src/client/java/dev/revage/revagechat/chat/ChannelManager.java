package dev.revage.revagechat.chat;

import dev.revage.revagechat.chat.model.MessageContext;
import dev.revage.revagechat.config.ConfigManager;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;

/**
 * Maintains channel-level state and routing decisions.
 */
public final class ChannelManager {
    private static final String DEFAULT_CHANNEL_ID = "default";

    private final ConfigManager configManager;
    private final Map<String, ChatChannel> channelsById;
    private final ArrayList<ChatChannel> routingTargetsScratch;
    private final RoutingResult routingResult;
    private final StringBuilder cloneTokenBuilder;

    public ChannelManager(ConfigManager configManager) {
        this.configManager = configManager;
        this.channelsById = new LinkedHashMap<>();
        this.routingTargetsScratch = new ArrayList<>(4);
        this.routingResult = new RoutingResult();
        this.cloneTokenBuilder = new StringBuilder(12);
    }

    public void load() {
        channelsById.clear();

        List<ChatChannel> persistedChannels = configManager.loadChannels();
        for (int i = 0, size = persistedChannels.size(); i < size; i++) {
            ChatChannel channel = persistedChannels.get(i);
            channelsById.put(channel.id(), channel);
        }

        ensureDefaultChannel();
    }

    public void save() {
        configManager.saveChannels(channelsById.values());
    }

    public ChatChannel createChannel(ChatChannel channel) {
        Objects.requireNonNull(channel, "channel");
        channelsById.put(channel.id(), channel);
        save();
        return channel;
    }

    public boolean removeChannel(String id) {
        if (DEFAULT_CHANNEL_ID.equals(id)) {
            return false;
        }

        boolean removed = channelsById.remove(id) != null;
        if (removed) {
            save();
        }

        return removed;
    }

    public Optional<ChatChannel> getChannel(String id) {
        return Optional.ofNullable(channelsById.get(id));
    }

    public Collection<ChatChannel> getChannels() {
        return Collections.unmodifiableCollection(channelsById.values());
    }

    public RoutingResult resolveRouting(MessageContext context) {
        routingTargetsScratch.clear();

        ChatChannel explicitChannel = resolveExplicitTarget(context.originalText());
        if (explicitChannel != null) {
            routingTargetsScratch.add(explicitChannel);
            routingResult.set(routingTargetsScratch, false);
            return routingResult;
        }

        boolean hideFromDefault = context.originalText().contains("@silent");

        if (!hideFromDefault) {
            ChatChannel defaultChannel = channelsById.get(DEFAULT_CHANNEL_ID);
            if (defaultChannel != null) {
                routingTargetsScratch.add(defaultChannel);
            }
        }

        appendCloneTargets(context.originalText());

        if (routingTargetsScratch.isEmpty()) {
            ChatChannel defaultChannel = channelsById.get(DEFAULT_CHANNEL_ID);
            if (defaultChannel != null) {
                routingTargetsScratch.add(defaultChannel);
                hideFromDefault = false;
            }
        }

        routingResult.set(routingTargetsScratch, hideFromDefault);
        return routingResult;
    }

    private ChatChannel resolveExplicitTarget(String rawMessage) {
        if (!rawMessage.startsWith("#")) {
            return null;
        }

        int splitIndex = rawMessage.indexOf(' ');
        if (splitIndex <= 1) {
            return null;
        }

        String channelId = rawMessage.substring(1, splitIndex);
        return channelsById.get(channelId);
    }

    private void appendCloneTargets(String rawMessage) {
        int start = rawMessage.indexOf("@clone:");
        if (start < 0) {
            return;
        }

        int index = start + 7;
        int length = rawMessage.length();

        cloneTokenBuilder.setLength(0);
        while (index < length) {
            char current = rawMessage.charAt(index);
            if (current == ' ') {
                break;
            }

            if (current == ',') {
                appendCloneTargetToken(cloneTokenBuilder);
                cloneTokenBuilder.setLength(0);
            } else {
                cloneTokenBuilder.append(current);
            }

            index++;
        }

        appendCloneTargetToken(cloneTokenBuilder);
    }

    private void appendCloneTargetToken(StringBuilder tokenBuilder) {
        if (tokenBuilder.isEmpty()) {
            return;
        }

        ChatChannel channel = channelsById.get(tokenBuilder.toString().trim());
        if (channel != null && !routingTargetsScratch.contains(channel)) {
            routingTargetsScratch.add(channel);
        }
    }

    private void ensureDefaultChannel() {
        channelsById.computeIfAbsent(
            DEFAULT_CHANNEL_ID,
            id -> new ChatChannel(id, "Default", 0xFFFFFF, 1.0F, 1.0F, WindowState.OPEN)
        );
    }
}
