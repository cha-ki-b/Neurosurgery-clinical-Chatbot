<%
    ui.includeCss("agentgateway", "agentgateway.css")
    ui.includeJavascript("agentgateway", "agent-chat.js")
%>

<script type="text/javascript">
    // Rendered through explicit fallbacks: an interpolation that came out null would emit
    // "var agentCanWrite = ;" and take the whole page's JavaScript down with it, on a screen
    // clinicians use for everything else.
    var agentPatientUuid = "${ patientUuid ?: '' }";
    var agentCanWrite = ${ canWrite ? 'true' : 'false' };
    var agentAutoFocus = false;
</script>

<div class="info-section agent-widget">
    <div class="info-header">
        <i class="icon-comments"></i>
        <h3>ASSISTANT CLINIQUE</h3>
        <i class="icon-share-alt edit-action right" title="Ouvrir en plein ecran"
           onclick="location.href='${ ui.pageLink("agentgateway", "chat", [patientId: patientUuid]) }';"></i>
    </div>
    <div class="info-body">
        <% if (!canUse) { %>
            <div class="agent-notice">
                Vous n'avez pas l'autorisation d'utiliser l'assistant clinique.
            </div>
        <% } else { %>

        <div id="agentMessages" class="agent-messages agent-messages-compact" aria-live="polite">
            <div class="agent-message agent-message-bot">
                Ce patient est deja selectionne. Demandez-moi son dossier, ou dictez ce qu'il
                faut enregistrer.
            </div>
        </div>

        <div id="agentPending" class="agent-pending" style="display:none;">
            <div class="agent-pending-title">Confirmation requise</div>
            <div id="agentPendingSummary" class="agent-pending-summary"></div>
            <div class="agent-pending-actions">
                <button type="button" class="agent-btn agent-btn-primary" onclick="agentConfirm()">Confirmer</button>
                <button type="button" class="agent-btn" onclick="agentCancel()">Annuler</button>
            </div>
        </div>

        <form class="agent-composer" onsubmit="return agentSend(event)">
            <input type="text" id="agentInput" autocomplete="off" placeholder="Votre demande&hellip;"/>
            <button type="submit" class="agent-btn agent-btn-primary" id="agentSendButton">&rsaquo;</button>
        </form>

        <% } %>
    </div>
</div>
