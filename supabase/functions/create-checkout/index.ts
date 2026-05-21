// Supabase Edge Function: create-checkout
// Datei: supabase/functions/create-checkout/index.ts
// 
// Diese Funktion läuft auf Supabase-Servern (nicht im Browser).
// Der Stripe Secret Key ist hier sicher gespeichert.

import { serve } from "https://deno.land/std@0.168.0/http/server.ts"

const STRIPE_SECRET_KEY = Deno.env.get('STRIPE_SECRET_KEY')!;
const SUPABASE_URL = Deno.env.get('SUPABASE_URL')!;
const SUPABASE_SERVICE_KEY = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;

// Deine Live-URL (nach GitHub Pages Deployment)
const SUCCESS_URL = 'https://romanbaumschlager-netizen.github.io/bauscout/success.html';
const CANCEL_URL  = 'https://romanbaumschlager-netizen.github.io/bauscout/';

serve(async (req) => {
  // CORS Headers für GitHub Pages
  const headers = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
    'Content-Type': 'application/json',
  };

  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers });
  }

  try {
    const { suchanfrage_id, kunden_id, email, firmenname, betrag_cent, beschreibung } = await req.json();

    if (!suchanfrage_id || !betrag_cent) {
      throw new Error('Pflichtfelder fehlen: suchanfrage_id, betrag_cent');
    }

    // Stripe Checkout Session erstellen
    const stripeResponse = await fetch('https://api.stripe.com/v1/checkout/sessions', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${STRIPE_SECRET_KEY}`,
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: new URLSearchParams({
        'mode': 'payment',
        'success_url': `${SUCCESS_URL}?session_id={CHECKOUT_SESSION_ID}&suchanfrage_id=${suchanfrage_id}`,
        'cancel_url': CANCEL_URL,
        'customer_email': email,
        'line_items[0][price_data][currency]': 'eur',
        'line_items[0][price_data][unit_amount]': betrag_cent.toString(),
        'line_items[0][price_data][product_data][name]': 'BauScout – Einmaliger Scout-Lauf',
        'line_items[0][price_data][product_data][description]': beschreibung,
        'line_items[0][quantity]': '1',
        'metadata[suchanfrage_id]': suchanfrage_id,
        'metadata[kunden_id]': kunden_id,
        'metadata[firmenname]': firmenname,
        // Stripe sendet nach Zahlung einen Webhook → startet den Agenten
        'payment_intent_data[metadata][suchanfrage_id]': suchanfrage_id,
        'payment_intent_data[metadata][kunden_id]': kunden_id,
      }).toString(),
    });

    const session = await stripeResponse.json();

    if (!session.url) {
      console.error('Stripe Fehler:', session);
      throw new Error(session.error?.message || 'Stripe Session konnte nicht erstellt werden');
    }

    // Suchanfrage-Status auf "checkout_gestartet" setzen
    await fetch(`${SUPABASE_URL}/rest/v1/suchanfragen?id=eq.${suchanfrage_id}`, {
      method: 'PATCH',
      headers: {
        'apikey': SUPABASE_SERVICE_KEY,
        'Authorization': `Bearer ${SUPABASE_SERVICE_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ 
        status: 'checkout_gestartet',
        stripe_session_id: session.id,
      }),
    });

    return new Response(JSON.stringify({ url: session.url, session_id: session.id }), { headers });

  } catch (error) {
    console.error('Edge Function Fehler:', error.message);
    return new Response(
      JSON.stringify({ error: error.message }),
      { status: 400, headers }
    );
  }
});
