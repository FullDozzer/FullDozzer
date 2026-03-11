package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

public final class PlayerFilter extends AbstractChannelScopedFilter {
    private final Set<String> blockedPlayers;

    public PlayerFilter(Set<String> blockedPlayers) {
        this.blockedPlayers = new HashSet<>(blockedPlayers.size());
        for (String player : blockedPlayers) {
            this.blockedPlayers.add(player.toLowerCase(Locale.ROOT));
        }
    }

    @Override
    public boolean matches(MessageContext ctx) {
        return blockedPlayers.contains(ctx.senderName().toLowerCase(Locale.ROOT));
    }

    @Override
    public void apply(MessageContext ctx) {
        FilterActionStore.block(ctx);
    }
}
