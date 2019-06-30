package com.javaproject.demo.controller;

import com.javaproject.demo.dto.AccesstokenDTO;
import com.javaproject.demo.dto.GithubUser;
import com.javaproject.demo.model.User;
import com.javaproject.demo.provider.GithupProvider;
import com.javaproject.demo.service.UserService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;

import javax.servlet.http.Cookie;
import javax.servlet.http.HttpServletResponse;
import java.util.UUID;

@Controller
public class AuthorizeController {

    @Autowired
    private GithupProvider githupProvider;

    @Value("${github.client.id}")//分离配置代码
        private String clientid;

    @Value("${github.client.secret}")
    private String   clientsecret;

    @Value("${github.redirect.uri}")
    private String redirecturi;
    @Autowired
    private UserService userService;
    @GetMapping("/callback")
    public String callback(@RequestParam(name = "code") String code,
                           @RequestParam(name = "state")String state,
                            HttpServletResponse response ){
        AccesstokenDTO accesstokenDTO =new AccesstokenDTO();
        accesstokenDTO.setClient_id(clientid);
        accesstokenDTO.setClient_secret(clientsecret);
        accesstokenDTO.setCode(code);
        accesstokenDTO.setRedirect_uri(redirecturi);
        accesstokenDTO.setState(state);
        String accessToken = githupProvider.getAccessToken(accesstokenDTO);
        GithubUser githubUser = githupProvider.getUser(accessToken);
        if (githubUser != null && githubUser.getId() != null) {
            User user = new User();
            String token=UUID.randomUUID().toString();
            user.setToken(token);
            user.setName(githubUser.getName());
            user.setAccountId(String.valueOf(githubUser.getId()));

            userService.createOrUpdate(user);
            //user.setAvatarUrl(githubUser.getAvatar_url());

            // 登录成功，写cookie 和session
            response.addCookie(new Cookie("token",token));
            return "redirect:/"; //redirect 重定向到我所制定的页面去

        }
        else {
            //失败  重新登陆
            return "redirect:/";
        }
    }

}
