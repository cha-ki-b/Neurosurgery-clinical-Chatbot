<%
    ui.decorateWith("appui", "standardEmrPage")
%>

${ ui.includeCss("agentgateway", "agentgateway.css") }
${ ui.includeJavascript("agentgateway", "agent-chat.js") }

<script type="text/javascript">
    var agentPatientUuid = "${ patientUuid ?: '' }";
    var agentCanWrite = ${ canWrite ? 'true' : 'false' };
    // This page exists only for the chat, so putting the cursor in the box is the right thing.
    var agentAutoFocus = true;
    var breadcrumbs = [
        { icon: "icon-home", link: '/' + OPENMRS_CONTEXT_PATH + '/index.htm' },
        { label: "Assistant clinique" }
    ];
</script>

<div class="agent-layout">
    <% if (accessDenied) { %>
        <div class="agent-panel">
            <div class="agent-panel-content">
                <div class="agent-notice">
                    Vous n'avez pas l'autorisation d'utiliser l'assistant clinique.
                    Contactez un administrateur si vous pensez que c'est une erreur.
                </div>
            </div>
        </div>
    <% } else { %>

    <div class="agent-panel">
        <div class="agent-panel-header">
            <h2>Assistant clinique</h2>
            <% if (patient) { %>
                <span class="agent-context-chip">
                    Patient&nbsp;: ${ (patient.familyName && patient.familyName.toString().trim().toLowerCase() != 'null') ? ui.format(patient.familyName) : '' }
                    ${ (patient.givenName && patient.givenName.toString().trim().toLowerCase() != 'null') ? ui.format(patient.givenName) : '' }
                </span>
            <% } %>
        </div>

        <div class="agent-panel-content">
            <% if (!canWrite) { %>
                <div class="agent-notice agent-notice-info">
                    Votre compte permet uniquement les consultations. L'assistant ne pourra rien enregistrer
                    en votre nom.
                </div>
            <% } %>

            <div id="agentMessages" class="agent-messages" aria-live="polite">
                <div class="agent-message agent-message-bot">
                    Bonjour. Je peux rechercher un patient, afficher son dossier, et &mdash; apr&egrave;s votre
                    confirmation explicite &mdash; cr&eacute;er ou mettre &agrave; jour un dossier.
                    Que souhaitez-vous faire&nbsp;?
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
                <input type="text" id="agentInput" autocomplete="off"
                       placeholder="Posez votre question ou d&eacute;crivez ce que vous voulez faire"/>
                <button type="submit" class="agent-btn agent-btn-primary" id="agentSendButton">Envoyer</button>
            </form>
        </div>
    </div>

    <% } %>
</div>
