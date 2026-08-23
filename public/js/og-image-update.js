// Dynamic OG Image Update
// NOTE: Social media crawlers (Facebook, Twitter) don't execute JavaScript,
// so they will always see the static OG tags above. To show dynamic OG images
// for route links, you would need Server-Side Rendering (SSR).
(function() {
    var params = new URLSearchParams(window.location.search);
    var from = params.get('from');
    var to = params.get('to');
    var mode = params.get('mode') || 'cheap';
    
    if (from && to) {
        var ogImageUrl = 'https://zaileprzeja.de/api/og-image?from=' + encodeURIComponent(from) + '&to=' + encodeURIComponent(to) + '&mode=' + encodeURIComponent(mode);
        document.querySelector('meta[property="og:image"]').setAttribute('content', ogImageUrl);
        document.querySelector('meta[name="twitter:image"]').setAttribute('content', ogImageUrl);
        
        // Update URL meta tag for client-side sharing
        document.querySelector('meta[property="og:url"]').setAttribute('content', window.location.href);
        
        // Update title with route info
        document.title = 'Za Ile Przejadę? - Trasa';
    }
})();
