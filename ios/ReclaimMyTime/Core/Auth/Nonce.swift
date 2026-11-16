import CryptoKit
import Foundation

/// Sign-in nonces.
///
/// - Google (T6.0, the live flow): `googleRaw()` is 32 random bytes, base64url; `POST
///   /auth/google/start` gets `sha256Base64URL(raw)` and `POST /auth/google/complete` gets the raw
///   value, binding the login to this app instance (login CSRF).
/// - Sign in with Apple (ARCH §6, kept but unused since T6.0): Apple receives SHA-256(raw) as
///   `request.nonce`; the server receives the raw nonce and checks the token's claim equals its
///   SHA-256 hex digest.
enum Nonce {
    private static let charset = Array("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-._")

    /// A random nonce drawn uniformly from a URL-safe charset. `SystemRandomNumberGenerator` is a
    /// CSPRNG on Apple platforms. 32 chars ≈ 190 bits; the contract requires ≥ 16.
    static func random(length: Int = 32) -> String {
        precondition(length >= 16, "nonce must be at least 16 characters")
        var generator = SystemRandomNumberGenerator()
        return String((0..<length).map { _ in charset[Int.random(in: 0..<charset.count, using: &generator)] })
    }

    /// Lowercase hex SHA-256 of the UTF-8 bytes.
    static func sha256Hex(_ input: String) -> String {
        SHA256.hash(data: Data(input.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    // MARK: Google sign-in

    /// `byteCount` random bytes from the system CSPRNG, base64url without padding (32 bytes → 43
    /// chars). Generated per attempt and kept only in memory.
    static func googleRaw(byteCount: Int = 32) -> String {
        precondition(byteCount >= 16, "nonce must carry at least 128 bits")
        var generator = SystemRandomNumberGenerator()
        return base64URL(Data((0..<byteCount).map { _ in UInt8.random(in: .min ... .max, using: &generator) }))
    }

    /// base64url (RFC 4648 §5, no padding) of SHA-256 over the UTF-8 bytes: the `nonce_hash` the
    /// server compares the raw nonce against.
    static func sha256Base64URL(_ input: String) -> String {
        base64URL(Data(SHA256.hash(data: Data(input.utf8))))
    }

    /// RFC 4648 §5 base64url without `=` padding.
    static func base64URL(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
