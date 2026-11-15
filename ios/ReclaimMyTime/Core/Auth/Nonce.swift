import CryptoKit
import Foundation

/// Sign in with Apple nonce handling (ARCH §6): Apple receives SHA-256(raw) as `request.nonce`;
/// the server receives the raw nonce and checks the token's claim equals its SHA-256 hex digest.
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
}
