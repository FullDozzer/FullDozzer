package dev.revage.revagechat.mixin;

import dev.revage.revagechat.RevageChatClient;
import dev.revage.revagechat.chat.MessageType;
import java.util.UUID;
import net.minecraft.client.gui.hud.ChatHud;
import net.minecraft.client.gui.hud.MessageIndicator;
import net.minecraft.network.message.MessageSignatureData;
import net.minecraft.text.Text;
import org.jetbrains.annotations.Nullable;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Minimal incoming message interception at ChatHud insertion point.
 *
 * <p>Does not replace ChatHud behavior; only conditionally cancels a single insertion.
 */
@Mixin(ChatHud.class)
public abstract class ChatHudMixin {

    @Inject(method = "addMessage(Lnet/minecraft/text/Text;)V", at = @At("HEAD"), cancellable = true)
    private void revagechat$onAddMessage(Text message, CallbackInfo ci) {
        if (!shouldAllow(message, "unknown", null, MessageType.INCOMING)) {
            ci.cancel();
        }
    }

    @Inject(
        method = "addMessage(Lnet/minecraft/text/Text;Lnet/minecraft/network/message/MessageSignatureData;Lnet/minecraft/client/gui/hud/MessageIndicator;)V",
        at = @At("HEAD"),
        cancellable = true
    )
    private void revagechat$onAddSignedMessage(
        Text message,
        MessageSignatureData signature,
        MessageIndicator indicator,
        CallbackInfo ci
    ) {
        if (!shouldAllow(message, "unknown", null, MessageType.INCOMING)) {
            ci.cancel();
        }
    }

    private boolean shouldAllow(Text message, String senderName, @Nullable UUID uuid, MessageType type) {
        String text = message == null ? "" : message.getString();

        return RevageChatClient.interceptIncomingFromMixin(text, text, senderName, uuid, type);
    }
}
