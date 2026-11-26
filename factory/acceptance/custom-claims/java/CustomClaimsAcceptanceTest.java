// Black-box acceptance tests for custom token-request fields (customClaims).
// Copied into an SDK checkout by run.sh. Contract: ../contract.md.
//
// The option is set by reflection so that this file compiles against an SDK
// without it; cases that need the option then fail with "option missing".
package dev.openfga.sdk.factoryacceptance;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.fail;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.openfga.sdk.api.client.OpenFgaClient;
import dev.openfga.sdk.api.client.model.ClientCheckRequest;
import dev.openfga.sdk.api.configuration.ClientConfiguration;
import dev.openfga.sdk.api.configuration.ClientCredentials;
import dev.openfga.sdk.api.configuration.Credentials;
import dev.openfga.sdk.errors.ApiException;
import java.io.IOException;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class CustomClaimsAcceptanceTest {
    private static final String OPTION = "customClaims";
    private static final String TOKEN = "factory-synthetic-oauth-token";
    private static final String STORE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV";
    private static final String MODEL_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW";
    private static final String ORG = "factory team/one & two";
    private static final ObjectMapper JSON = new ObjectMapper();

    private record Case(String id, Map<String, String> claims, String scopes, boolean audience, Map<String, String> extra) {}

    private static Map<String, String> reserved() {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("organization", ORG);
        m.put("client_id", "wrong");
        m.put("client_secret", "wrong");
        m.put("grant_type", "wrong");
        m.put("audience", "wrong");
        return m;
    }

    private static Map<String, String> custom() {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("organization", ORG);
        m.put("scope", "read:tuples");
        return m;
    }

    private static Case caseFor(String id) {
        return switch (id) {
            case "default", "denied" -> new Case(id, null, null, true, Map.of());
            case "empty" -> new Case(id, new LinkedHashMap<>(), null, true, Map.of());
            case "custom" -> new Case(id, custom(), null, true, Map.of("organization", ORG, "scope", "read:tuples"));
            case "reserved" -> new Case(id, reserved(), null, true, Map.of("organization", ORG));
            case "scopes" -> new Case(
                    id,
                    custom(),
                    "write:tuples list:objects",
                    true,
                    Map.of("organization", ORG, "scope", "write:tuples list:objects"));
            case "empty-scopes" -> new Case(
                    id, custom(), "", true, Map.of("organization", ORG, "scope", "read:tuples"));
            case "no-audience" -> new Case(id, reserved(), null, false, Map.of("organization", ORG));
            default -> throw new IllegalArgumentException(id);
        };
    }

    private record TokenRequest(Map<String, String> form, String contentType) {}

    private record ApiRequest(String user, boolean bearerValid) {}

    private static final class Fixture implements AutoCloseable {
        final boolean deny;
        final List<TokenRequest> tokens = new ArrayList<>();
        final List<ApiRequest> apis = new ArrayList<>();
        final List<String> problems = new ArrayList<>();
        final HttpServer server;
        final ExecutorService executor = Executors.newCachedThreadPool();

        Fixture(boolean deny) throws IOException {
            this.deny = deny;
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/", this::handle);
            server.setExecutor(executor);
            server.start();
        }

        String url() {
            return "http://127.0.0.1:" + server.getAddress().getPort();
        }

        private synchronized void handle(HttpExchange exchange) throws IOException {
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            String path = exchange.getRequestURI().getPath();
            boolean post = "POST".equals(exchange.getRequestMethod());
            if (post && path.equals("/oauth/token")) {
                Map<String, String> form = new LinkedHashMap<>();
                for (String pair : body.isEmpty() ? new String[0] : body.split("&")) {
                    int eq = pair.indexOf('=');
                    String key = URLDecoder.decode(eq < 0 ? pair : pair.substring(0, eq), StandardCharsets.UTF_8);
                    String value = eq < 0 ? "" : URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8);
                    if (form.put(key, value) != null) {
                        problems.add("duplicate token form key " + key);
                    }
                }
                tokens.add(new TokenRequest(form, String.valueOf(exchange.getRequestHeaders().getFirst("Content-Type"))));
                if (deny) {
                    reply(exchange, 401, "{\"error\":\"invalid_client\"}");
                } else {
                    reply(exchange, 200,
                            "{\"access_token\":\"" + TOKEN + "\",\"expires_in\":3600,\"token_type\":\"Bearer\"}");
                }
            } else if (post && path.equals("/stores/" + STORE_ID + "/check")) {
                String user = JSON.readTree(body).path("tuple_key").path("user").asText();
                boolean valid = ("Bearer " + TOKEN).equals(exchange.getRequestHeaders().getFirst("Authorization"));
                apis.add(new ApiRequest(user, valid));
                if (!valid) {
                    reply(exchange, 401, "{\"code\":\"unauthenticated\"}");
                } else {
                    reply(exchange, 200, "{\"allowed\":" + "user:anne".equals(user) + "}");
                }
            } else {
                problems.add("unexpected route " + exchange.getRequestMethod() + " " + path);
                reply(exchange, 404, "{\"code\":\"not_found\"}");
            }
        }

        private static void reply(HttpExchange exchange, int status, String json) throws IOException {
            byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        }

        @Override
        public void close() {
            server.stop(0);
            executor.shutdownNow();
        }
    }

    private static void setCustomClaims(ClientCredentials credentials, Map<String, String> claims) throws Exception {
        Method method;
        try {
            method = ClientCredentials.class.getMethod(OPTION, Map.class);
        } catch (NoSuchMethodException e) {
            fail("option missing: ClientCredentials." + OPTION + "(Map<String, String>)");
            return;
        }
        try {
            method.invoke(credentials, claims);
        } catch (InvocationTargetException e) {
            throw new AssertionError("ClientCredentials." + OPTION + " threw", e.getCause());
        }
    }

    private static Integer statusOf(Throwable error) {
        for (Throwable t = error; t != null; t = t.getCause()) {
            if (t instanceof ApiException api) {
                return api.getStatusCode();
            }
        }
        return null;
    }

    private static Map<String, String> expectedForm(Case c) {
        Map<String, String> form = new LinkedHashMap<>();
        form.put("client_id", "factory-client");
        form.put("client_secret", "factory-secret");
        form.put("grant_type", "client_credentials");
        if (c.audience()) {
            form.put("audience", "factory-audience");
        }
        if (c.scopes() != null && !c.scopes().isBlank()) {
            form.put("scope", c.scopes());
        }
        form.putAll(c.extra());
        return form;
    }

    private static boolean check(OpenFgaClient client, String user) throws Exception {
        return client.check(new ClientCheckRequest()
                        .user(user)
                        .relation("viewer")
                        ._object("document:factory"))
                .get()
                .getAllowed();
    }

    @ParameterizedTest(name = "{0}")
    @ValueSource(
            strings = {"default", "empty", "custom", "reserved", "denied", "scopes", "empty-scopes", "no-audience"})
    void customClaims(String id) throws Exception {
        Case c = caseFor(id);
        boolean denied = id.equals("denied");
        Map<String, String> snapshot = c.claims() == null ? null : new LinkedHashMap<>(c.claims());

        try (Fixture fixture = new Fixture(denied)) {
            ClientCredentials credentials = new ClientCredentials()
                    .clientId("factory-client")
                    .clientSecret("factory-secret")
                    .apiTokenIssuer(fixture.url() + "/oauth/token");
            if (c.audience()) {
                credentials.apiAudience("factory-audience");
            }
            if (c.scopes() != null) {
                credentials.scopes(c.scopes());
            }
            if (c.claims() != null) {
                setCustomClaims(credentials, c.claims());
            }
            ClientConfiguration configuration = new ClientConfiguration()
                    .apiUrl(fixture.url())
                    .storeId(STORE_ID)
                    .authorizationModelId(MODEL_ID)
                    .credentials(new Credentials(credentials))
                    .maxRetries(0);
            OpenFgaClient client = new OpenFgaClient(configuration);

            if (denied) {
                Integer status = null;
                try {
                    check(client, "user:anne");
                    fail("denied: expected a token error");
                } catch (Exception e) {
                    status = statusOf(e);
                }
                assertEquals(401, status, "denied: token error status");
            } else {
                assertTrue(check(client, "user:anne"), "user:anne must be allowed");
                assertFalse(check(client, "user:bob"), "user:bob must be denied");
            }

            synchronized (fixture) {
                assertEquals(List.of(), fixture.problems);
                assertEquals(snapshot, c.claims(), "caller's custom claims map was mutated");
                if (denied) {
                    assertEquals(List.of(), fixture.apis, "denied: no API requests");
                    assertFalse(fixture.tokens.isEmpty(), "denied: a token request was made");
                } else {
                    assertEquals(
                            List.of(new ApiRequest("user:anne", true), new ApiRequest("user:bob", true)),
                            fixture.apis);
                    assertEquals(1, fixture.tokens.size(), "token must be exchanged once and cached");
                }
                for (TokenRequest token : fixture.tokens) {
                    assertNotNull(token.contentType());
                    assertTrue(
                            token.contentType().startsWith("application/x-www-form-urlencoded"),
                            "content type " + token.contentType());
                    assertEquals(expectedForm(c), token.form());
                }
            }
        }
    }
}
