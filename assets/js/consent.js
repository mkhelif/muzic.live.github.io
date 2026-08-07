/*
 * Silktide consent manager — configuration.
 */
window.silktideConsentManager.init({
    backdrop: {
        show: true
    },
    icon: {
        position: "bottomLeft"
    },
    prompt: {
        position: "bottomLeft"
    },
    consentTypes: [
        {
            id: "essentiel",
            label: "Essentiels",
            description: "Ces cookies sont indispensables au bon fonctionnement du site web et ne peuvent pas être désactivés. Ils permettent notamment de se connecter et de paramétrer ses préférences de confidentialité.",
            required: true
        },
        {
            id: "analytics",
            label: "Analytics",
            description: "Ces cookies nous aident à améliorer le site en suivant quelles pages sont les plus populaires et comment les visiteurs se déplacent sur le site.",
            required: false,
            gtag: "analytics_storage"
        },
        {
            id: "marketing",
            label: "Marketing",
            description: "Ces cookies sont utilisés par nous et nos partenaires publicitaires pour vous montrer des publicités pertinentes sur ce site et ailleurs, et pour mesurer les performances de ces campagnes.",
            required: false,
            gtag: [
                "ad_storage",
                "ad_user_data",
                "ad_personalization"
            ]
        }
    ],
    text: {
        prompt: {
            description: "Nous utilisons des cookies sur notre site pour améliorer votre expérience utilisateur, vous proposer un contenu personnalisé et analyser notre trafic.",
            acceptAllButtonText: "Tout accepter",
            acceptAllButtonAccessibleLabel: "Accepter tous les cookies",
            rejectNonEssentialButtonText: "Seulement nécessaires",
            rejectNonEssentialButtonAccessibleLabel: "Accepter uniquement les cookies nécessaires",
            preferencesButtonText: "Préférences",
            preferencesButtonAccessibleLabel: "Montrer les préférences de gestion des cookies"
        },
        preferences: {
            title: "Personnalisez vos préférences en matière de cookies",
            description: "Nous respectons votre droit à la vie privée. Vous pouvez choisir de ne pas autoriser certains types de cookies. Vos préférences en matière de cookies s'appliqueront sur l'ensemble de notre site web.",
            saveButtonText: "Enregistrer et fermer",
            saveButtonAccessibleLabel: "Enregistrer vos préférences de gestion des cookies",
            creditLinkText: "-",
            creditLinkAccessibleLabel: "-"
        }
    }
});
