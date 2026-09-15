/* crackme.c —— 多会话逆向演练样本：XOR 变换 + 长度校验
 * 正确输入即 flag："flag{0rch3str4t0r_0k}"（21 字节，逐字节 XOR 0x5A 与密文比较）
 * 运行：echo <input> | ./crackme
 */
#include <stdio.h>
#include <string.h>

static const char enc[] = {
    0x3c, 0x36, 0x3b, 0x3d, 0x21, 0x6a, 0x28, 0x39, 0x32, 0x69,
    0x29, 0x2e, 0x28, 0x6e, 0x2e, 0x6a, 0x28, 0x05, 0x6a, 0x31,
    0x27, 0x00
};

int check_flag(const char *input) {
    size_t n = strlen(input);
    if (n != strlen(enc)) return 0;
    for (size_t i = 0; i < n; i++)
        if ((input[i] ^ 0x5A) != (unsigned char)enc[i]) return 0;
    return 1;
}

int main(void) {
    char buf[128] = {0};
    if (!fgets(buf, sizeof(buf), stdin)) {
        puts("[-] no input");
        return 1;
    }
    buf[strcspn(buf, "\r\n")] = 0;
    if (check_flag(buf)) {
        puts("[+] Correct! Access granted.");
        return 0;
    }
    puts("[-] Wrong input.");
    return 1;
}
